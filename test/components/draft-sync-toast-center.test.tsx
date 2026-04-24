/**
 * Q.6 #300 (2026-04-24, checkbox 4) — DraftSyncToastCenter tests.
 *
 * Subscribes to the ``onDraftSynced`` bus and surfaces a toast
 *「從他裝置同步了草稿」. Driven by calling ``emitDraftSynced`` directly
 * (mirrors the ``ApiErrorToastCenter`` test pattern of driving the
 * real bus without stubbing the writer side).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { DraftSyncToastCenter } from "@/components/omnisight/draft-sync-toast-center"
import {
  _resetDraftSyncListenersForTests,
  emitDraftSynced,
} from "@/lib/draft-sync-bus"

beforeEach(() => {
  _resetDraftSyncListenersForTests()
})

afterEach(() => {
  _resetDraftSyncListenersForTests()
  vi.useRealTimers()
})

describe("DraftSyncToastCenter", () => {
  it("renders nothing when the bus is idle", () => {
    render(<DraftSyncToastCenter />)
    expect(screen.queryByTestId("draft-sync-toast-center")).not.toBeInTheDocument()
  })

  it("renders a toast「從他裝置同步了草稿」when a draft_synced event fires", async () => {
    render(<DraftSyncToastCenter />)
    await act(async () => {
      emitDraftSynced({
        slotKey: "invoke:main",
        content: "hello from peer",
        remoteUpdatedAt: 2000,
        localUpdatedAt: 1000,
      })
    })
    const toast = await screen.findByTestId("draft-sync-toast-invoke:main")
    expect(toast).toBeInTheDocument()
    expect(screen.getByText("從他裝置同步了草稿")).toBeInTheDocument()
    expect(screen.getByText("DRAFT SYNC")).toBeInTheDocument()
    expect(screen.getByText(/INVOKE 指令輸入框/)).toBeInTheDocument()
  })

  it("maps chat:main to the workspace chat label", async () => {
    render(<DraftSyncToastCenter />)
    await act(async () => {
      emitDraftSynced({
        slotKey: "chat:main",
        content: "msg",
        remoteUpdatedAt: 5,
        localUpdatedAt: null,
      })
    })
    expect(await screen.findByTestId("draft-sync-toast-chat:main")).toBeInTheDocument()
    expect(screen.getByText(/Workspace chat 輸入框/)).toBeInTheDocument()
  })

  it("dismiss button clears the toast", async () => {
    const user = userEvent.setup()
    render(<DraftSyncToastCenter />)
    await act(async () => {
      emitDraftSynced({
        slotKey: "invoke:main",
        content: "x",
        remoteUpdatedAt: 1,
        localUpdatedAt: null,
      })
    })
    expect(await screen.findByTestId("draft-sync-toast-invoke:main")).toBeInTheDocument()
    await user.click(screen.getByTestId("draft-sync-toast-dismiss-invoke:main"))
    expect(screen.queryByTestId("draft-sync-toast-invoke:main")).not.toBeInTheDocument()
  })

  it("coalesces repeated events on the same slot into a single toast", async () => {
    render(<DraftSyncToastCenter />)
    await act(async () => {
      emitDraftSynced({
        slotKey: "invoke:main",
        content: "first",
        remoteUpdatedAt: 1,
        localUpdatedAt: null,
      })
      emitDraftSynced({
        slotKey: "invoke:main",
        content: "second",
        remoteUpdatedAt: 2,
        localUpdatedAt: 1,
      })
    })
    const hits = await screen.findAllByTestId("draft-sync-toast-invoke:main")
    expect(hits).toHaveLength(1)
  })

  it("auto-dismisses after the timeout window", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    render(<DraftSyncToastCenter />)
    await act(async () => {
      emitDraftSynced({
        slotKey: "invoke:main",
        content: "x",
        remoteUpdatedAt: 1,
        localUpdatedAt: null,
      })
    })
    expect(await screen.findByTestId("draft-sync-toast-invoke:main")).toBeInTheDocument()
    await act(async () => {
      vi.advanceTimersByTime(6500)
    })
    expect(screen.queryByTestId("draft-sync-toast-invoke:main")).not.toBeInTheDocument()
  })

  it("keeps separate slots on separate toasts", async () => {
    render(<DraftSyncToastCenter />)
    await act(async () => {
      emitDraftSynced({
        slotKey: "invoke:main",
        content: "a",
        remoteUpdatedAt: 1,
        localUpdatedAt: null,
      })
      emitDraftSynced({
        slotKey: "chat:main",
        content: "b",
        remoteUpdatedAt: 2,
        localUpdatedAt: null,
      })
    })
    expect(await screen.findByTestId("draft-sync-toast-invoke:main")).toBeInTheDocument()
    expect(await screen.findByTestId("draft-sync-toast-chat:main")).toBeInTheDocument()
  })
})
