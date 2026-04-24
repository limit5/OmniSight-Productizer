/**
 * Q.7 #301 — Conflict409ToastCenter tests.
 *
 * Drives the bus with ``emitConflict409`` (same pattern as the
 * DraftSyncToastCenter tests) and asserts:
 *   - Idle → nothing rendered.
 *   - 409 event → orange CONFLICT 409 toast with hint + version line.
 *   - 重載 / 覆蓋 / 合併 buttons wired to the event's resolution
 *     handlers (重載 always present; the others hide when unwired).
 *   - Same-resource coalesce — two events on the same resource render
 *     one toast, not two.
 *   - Dismiss button removes the toast.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { Conflict409ToastCenter } from "@/components/omnisight/conflict-409-toast-center"
import {
  _resetConflict409ListenersForTests,
  emitConflict409,
} from "@/lib/conflict-409-bus"

beforeEach(() => {
  _resetConflict409ListenersForTests()
})

afterEach(() => {
  _resetConflict409ListenersForTests()
  vi.useRealTimers()
})

function _makeEvent(overrides: Partial<Parameters<typeof emitConflict409>[0]> = {}) {
  return {
    id: `c-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    resource: "task",
    currentVersion: 5,
    yourVersion: 2,
    hint: "另一裝置已修改，請重載",
    onReload: vi.fn(),
    ...overrides,
  }
}

describe("Conflict409ToastCenter", () => {
  it("renders nothing when the bus is idle", () => {
    render(<Conflict409ToastCenter />)
    expect(screen.queryByTestId("conflict-409-toast-center")).not.toBeInTheDocument()
  })

  it("renders CONFLICT 409 toast with hint + version delta", async () => {
    render(<Conflict409ToastCenter />)
    await act(async () => {
      emitConflict409(_makeEvent())
    })
    const toast = await screen.findByTestId("conflict-409-toast-task")
    expect(toast).toBeInTheDocument()
    expect(screen.getByText("CONFLICT 409")).toBeInTheDocument()
    expect(screen.getByText("另一裝置已修改，請重載")).toBeInTheDocument()
    expect(screen.getByText(/伺服器版本 5/)).toBeInTheDocument()
    expect(screen.getByText(/您的版本 2/)).toBeInTheDocument()
  })

  it("shows 重載 always; hides 覆蓋 / 合併 when handlers missing", async () => {
    render(<Conflict409ToastCenter />)
    await act(async () => {
      emitConflict409(_makeEvent()) // only onReload set
    })
    await screen.findByTestId("conflict-409-toast-task")
    expect(screen.getByTestId("conflict-409-reload-task")).toBeInTheDocument()
    expect(screen.queryByTestId("conflict-409-overwrite-task")).not.toBeInTheDocument()
    expect(screen.queryByTestId("conflict-409-merge-task")).not.toBeInTheDocument()
  })

  it("shows 覆蓋 and 合併 when handlers provided", async () => {
    render(<Conflict409ToastCenter />)
    await act(async () => {
      emitConflict409(_makeEvent({
        onOverwrite: vi.fn(),
        onMerge: vi.fn(),
      }))
    })
    await screen.findByTestId("conflict-409-toast-task")
    expect(screen.getByTestId("conflict-409-reload-task")).toBeInTheDocument()
    expect(screen.getByTestId("conflict-409-overwrite-task")).toBeInTheDocument()
    expect(screen.getByTestId("conflict-409-merge-task")).toBeInTheDocument()
  })

  it("clicking 重載 invokes onReload and dismisses the toast", async () => {
    const user = userEvent.setup()
    const onReload = vi.fn()
    render(<Conflict409ToastCenter />)
    await act(async () => {
      emitConflict409(_makeEvent({ onReload }))
    })
    await screen.findByTestId("conflict-409-toast-task")
    await user.click(screen.getByTestId("conflict-409-reload-task"))
    await waitFor(() => {
      expect(onReload).toHaveBeenCalledTimes(1)
    })
    expect(screen.queryByTestId("conflict-409-toast-task")).not.toBeInTheDocument()
  })

  it("clicking 覆蓋 invokes onOverwrite and dismisses the toast", async () => {
    const user = userEvent.setup()
    const onOverwrite = vi.fn()
    render(<Conflict409ToastCenter />)
    await act(async () => {
      emitConflict409(_makeEvent({ onOverwrite }))
    })
    await screen.findByTestId("conflict-409-toast-task")
    await user.click(screen.getByTestId("conflict-409-overwrite-task"))
    await waitFor(() => {
      expect(onOverwrite).toHaveBeenCalledTimes(1)
    })
    expect(screen.queryByTestId("conflict-409-toast-task")).not.toBeInTheDocument()
  })

  it("clicking 合併 invokes onMerge and dismisses the toast", async () => {
    const user = userEvent.setup()
    const onMerge = vi.fn()
    render(<Conflict409ToastCenter />)
    await act(async () => {
      emitConflict409(_makeEvent({ onMerge }))
    })
    await screen.findByTestId("conflict-409-toast-task")
    await user.click(screen.getByTestId("conflict-409-merge-task"))
    await waitFor(() => {
      expect(onMerge).toHaveBeenCalledTimes(1)
    })
    expect(screen.queryByTestId("conflict-409-toast-task")).not.toBeInTheDocument()
  })

  it("same-resource events coalesce into one toast (latest handlers)", async () => {
    render(<Conflict409ToastCenter />)
    const first = vi.fn()
    const second = vi.fn()
    await act(async () => {
      emitConflict409(_makeEvent({ onReload: first }))
      emitConflict409(_makeEvent({ onReload: second }))
    })
    // One toast visible; its reload button wires to the LATEST event
    // (second), not the stale (first). Proves coalesce uses the newer
    // event's handlers.
    const toasts = screen.getAllByTestId("conflict-409-toast-task")
    expect(toasts).toHaveLength(1)
    const user = userEvent.setup()
    await user.click(screen.getByTestId("conflict-409-reload-task"))
    await waitFor(() => {
      expect(second).toHaveBeenCalledTimes(1)
    })
    expect(first).not.toHaveBeenCalled()
  })

  it("different-resource events stack side-by-side", async () => {
    render(<Conflict409ToastCenter />)
    await act(async () => {
      emitConflict409(_makeEvent({ resource: "task" }))
      emitConflict409(_makeEvent({ resource: "runtime_settings" }))
    })
    expect(await screen.findByTestId("conflict-409-toast-task")).toBeInTheDocument()
    expect(screen.getByTestId("conflict-409-toast-runtime_settings")).toBeInTheDocument()
  })

  it("X button dismisses without invoking any handler", async () => {
    const user = userEvent.setup()
    const onReload = vi.fn()
    render(<Conflict409ToastCenter />)
    await act(async () => {
      emitConflict409(_makeEvent({ onReload }))
    })
    await screen.findByTestId("conflict-409-toast-task")
    await user.click(screen.getByTestId("conflict-409-dismiss-task"))
    expect(screen.queryByTestId("conflict-409-toast-task")).not.toBeInTheDocument()
    expect(onReload).not.toHaveBeenCalled()
  })

  it("localises the resource label (runtime_settings → Runtime 設定)", async () => {
    render(<Conflict409ToastCenter />)
    await act(async () => {
      emitConflict409(_makeEvent({ resource: "runtime_settings" }))
    })
    await screen.findByTestId("conflict-409-toast-runtime_settings")
    expect(screen.getByText(/Runtime 設定/)).toBeInTheDocument()
  })
})
