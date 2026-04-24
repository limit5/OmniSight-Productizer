/**
 * Q.7 #301 — conflict-409 bus + handleConflict409 contract.
 *
 * Exercises the parse + emit + dedupe surface that ``use409Conflict``
 * and ``<Conflict409ToastCenter />`` rely on. We avoid constructing
 * real ``ApiError`` instances via network — instead we import the
 * class and build the test doubles directly so the bus logic is
 * exercised without a ``fetch()`` roundtrip.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { ApiError } from "@/lib/api"
import {
  _resetConflict409ListenersForTests,
  emitConflict409,
  handleConflict409,
  onConflict409,
  parseConflictBody,
  type Conflict409Event,
} from "@/lib/conflict-409-bus"

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

describe("conflict-409-bus — parseConflictBody", () => {
  it("returns null for non-409 errors", () => {
    const err = new ApiError({
      kind: "bad_request",
      status: 400,
      body: "{}",
      parsed: {},
      traceId: null,
      path: "/",
      method: "GET",
    })
    expect(parseConflictBody(err)).toBeNull()
  })

  it("returns null for 409 without Q.7 body shape", () => {
    const err = _make409({ message: "plain 409" })
    expect(parseConflictBody(err)).toBeNull()
  })

  it("parses {detail:{current_version, your_version, hint, resource}}", () => {
    const err = _make409({
      detail: {
        current_version: 5,
        your_version: 2,
        hint: "另一裝置已修改",
        resource: "task",
      },
    })
    expect(parseConflictBody(err)).toEqual({
      resource: "task",
      currentVersion: 5,
      yourVersion: 2,
      hint: "另一裝置已修改",
    })
  })

  it("falls back to default hint when server omits it", () => {
    const err = _make409({
      detail: {
        current_version: 3,
        your_version: 0,
        resource: "tenant_secret",
      },
    })
    const parsed = parseConflictBody(err)
    expect(parsed?.hint).toBe("另一裝置已修改，請重載")
    expect(parsed?.resource).toBe("tenant_secret")
  })

  it("coerces null current_version (server could not re-read)", () => {
    const err = _make409({
      detail: {
        current_version: null,
        your_version: 0,
        resource: "task",
      },
    })
    const parsed = parseConflictBody(err)
    expect(parsed?.currentVersion).toBeNull()
  })
})

describe("conflict-409-bus — subscribe / emit", () => {
  it("delivers events to registered listeners", () => {
    const seen: Conflict409Event[] = []
    const off = onConflict409((evt) => seen.push(evt))
    emitConflict409({
      id: "e-1",
      resource: "task",
      currentVersion: 1,
      yourVersion: 0,
      hint: "h",
      onReload: () => {},
    })
    expect(seen).toHaveLength(1)
    expect(seen[0].resource).toBe("task")
    off()
  })

  it("respects unsubscribe — listener not called after off()", () => {
    const seen: Conflict409Event[] = []
    const off = onConflict409((evt) => seen.push(evt))
    off()
    emitConflict409({
      id: "e-2",
      resource: "task",
      currentVersion: 1,
      yourVersion: 0,
      hint: "h",
      onReload: () => {},
    })
    expect(seen).toHaveLength(0)
  })

  it("fans out to multiple subscribers", () => {
    const a: Conflict409Event[] = []
    const b: Conflict409Event[] = []
    onConflict409((evt) => a.push(evt))
    onConflict409((evt) => b.push(evt))
    emitConflict409({
      id: "e-3",
      resource: "task",
      currentVersion: 2,
      yourVersion: 1,
      hint: "h",
      onReload: () => {},
    })
    expect(a).toHaveLength(1)
    expect(b).toHaveLength(1)
  })

  it("one throwing listener does not starve the others", () => {
    const quiet: Conflict409Event[] = []
    onConflict409(() => { throw new Error("boom") })
    onConflict409((evt) => quiet.push(evt))
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {})
    emitConflict409({
      id: "e-4",
      resource: "task",
      currentVersion: 1,
      yourVersion: 0,
      hint: "h",
      onReload: () => {},
    })
    expect(quiet).toHaveLength(1)
    expect(warnSpy).toHaveBeenCalled()
  })
})

describe("handleConflict409 — catch-branch convenience", () => {
  it("returns false for non-409 errors (caller re-throws)", () => {
    const err = new Error("random")
    const handled = handleConflict409(err, { onReload: () => {} })
    expect(handled).toBe(false)
  })

  it("returns false for 409 without Q.7 body shape", () => {
    const err = _make409({ message: "plain 409" })
    const handled = handleConflict409(err, { onReload: () => {} })
    expect(handled).toBe(false)
  })

  it("emits on Q.7-shaped 409 and returns true", () => {
    const err = _make409({
      detail: {
        current_version: 3,
        your_version: 1,
        hint: "conflict!",
        resource: "runtime_settings",
      },
    })
    const seen: Conflict409Event[] = []
    onConflict409((evt) => seen.push(evt))
    const onReload = vi.fn()
    const onOverwrite = vi.fn()
    const handled = handleConflict409(err, { onReload, onOverwrite })
    expect(handled).toBe(true)
    expect(seen).toHaveLength(1)
    expect(seen[0].resource).toBe("runtime_settings")
    expect(seen[0].currentVersion).toBe(3)
    expect(seen[0].yourVersion).toBe(1)
    expect(seen[0].hint).toBe("conflict!")
    // Resolution handlers flow through unchanged so the toast can
    // invoke them without re-doing the parse.
    expect(seen[0].onReload).toBe(onReload)
    expect(seen[0].onOverwrite).toBe(onOverwrite)
    // Merge was not supplied — the bus event carries undefined so
    // the toast center knows to hide the 合併 button.
    expect(seen[0].onMerge).toBeUndefined()
  })

  it("returns false for thrown primitives (string / null)", () => {
    expect(handleConflict409(null, { onReload: () => {} })).toBe(false)
    expect(handleConflict409("oops", { onReload: () => {} })).toBe(false)
  })
})
