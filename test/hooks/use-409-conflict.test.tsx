/**
 * Q.7 #301 — ``use409Conflict`` hook contract.
 *
 * The hook is a thin wrapper over ``handleConflict409`` — tests just
 * assert the wrapper returns a stable callable and forwards the
 * arguments correctly. (The rich parse / emit surface is covered by
 * ``test/lib/conflict-409-bus.test.ts``.)
 */
import { renderHook } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { ApiError } from "@/lib/api"
import {
  _resetConflict409ListenersForTests,
  onConflict409,
  type Conflict409Event,
} from "@/lib/conflict-409-bus"
import { use409Conflict } from "@/hooks/use-409-conflict"

function _make409(body: Record<string, unknown>): ApiError {
  return new ApiError({
    kind: "conflict",
    status: 409,
    body: JSON.stringify(body),
    parsed: body,
    traceId: null,
    path: "/api/v1/tasks/task-1",
    method: "PATCH",
  })
}

beforeEach(() => {
  _resetConflict409ListenersForTests()
})

afterEach(() => {
  _resetConflict409ListenersForTests()
  vi.restoreAllMocks()
})

describe("use409Conflict", () => {
  it("returns a handle() callable", () => {
    const { result } = renderHook(() => use409Conflict())
    expect(typeof result.current.handle).toBe("function")
  })

  it("returns false for non-409 errors", () => {
    const { result } = renderHook(() => use409Conflict())
    const handled = result.current.handle(new Error("boom"), {
      onReload: () => {},
    })
    expect(handled).toBe(false)
  })

  it("emits + returns true on Q.7-shaped 409", () => {
    const { result } = renderHook(() => use409Conflict())
    const seen: Conflict409Event[] = []
    onConflict409((evt) => seen.push(evt))
    const err = _make409({
      detail: {
        current_version: 2,
        your_version: 0,
        hint: "conflict",
        resource: "task",
      },
    })
    const onReload = vi.fn()
    const handled = result.current.handle(err, { onReload })
    expect(handled).toBe(true)
    expect(seen).toHaveLength(1)
    expect(seen[0].onReload).toBe(onReload)
  })

  it("stable callable across re-renders (useMemo contract)", () => {
    const { result, rerender } = renderHook(() => use409Conflict())
    const first = result.current.handle
    rerender()
    expect(result.current.handle).toBe(first)
  })
})
