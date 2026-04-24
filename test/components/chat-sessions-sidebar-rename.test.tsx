/**
 * ZZ.B2 #304-2 checkbox 2 — operator rename UI tests.
 *
 * Locks the fallback-chain contract on the frontend side:
 *  - Clicking the rename pencil swaps the row into an inline input
 *    prefilled with the effective title (auto_title when that's the
 *    current source; empty when only a hash is showing).
 *  - Submitting a non-empty value calls `renameChatSession(sid, title)`
 *    and optimistically flips the row to the new `user_title` — the
 *    auto_title that lived underneath is preserved for future reverts.
 *  - Submitting an empty value calls `renameChatSession(sid, null)` so
 *    the backend clears `user_title` and the sidebar falls back to
 *    `auto_title` / hash per the 3-step chain.
 *  - ESC cancels without calling the API.
 *  - API failure rolls the optimistic update back and surfaces an
 *    inline error.
 *  - An incoming `session.titled` with `source="user"` renames the
 *    matching row in-place without a refetch (SSE fan-out across
 *    devices).
 */

import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, screen, act, fireEvent } from "@testing-library/react"

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>()
  return {
    ...actual,
    subscribeEvents: vi.fn(),
    fetchChatSessions: vi.fn(),
    renameChatSession: vi.fn(),
  }
})

import * as api from "@/lib/api"
import {
  ChatSessionsSidebar,
} from "@/components/omnisight/chat-sessions-sidebar"
import { primeSSE } from "../helpers/sse"

beforeEach(() => {
  vi.clearAllMocks()
})

function makeItem(
  overrides: Partial<api.ChatSessionItem> = {},
): api.ChatSessionItem {
  return {
    session_id: "sess-abc123",
    user_id: "u-1",
    tenant_id: "t-default",
    metadata: {},
    created_at: 100,
    updated_at: 100,
    ...overrides,
  }
}

describe("ChatSessionsSidebar rename UI", () => {
  it("pencil opens inline input prefilled with current auto_title", async () => {
    primeSSE(api)
    await act(async () => {
      render(
        <ChatSessionsSidebar
          initialSessions={[
            makeItem({
              session_id: "sess-1",
              metadata: { auto_title: "LLM label" },
            }),
          ]}
        />,
      )
    })

    act(() => {
      screen.getByTestId("chat-session-rename-sess-1").click()
    })

    const input = screen.getByTestId(
      "chat-session-rename-input-sess-1",
    ) as HTMLInputElement
    // Prefill is the effective (non-hash) title so the operator edits
    // in-place instead of retyping from scratch.
    expect(input.defaultValue).toBe("LLM label")
  })

  it("pencil on a hash-only row opens input with empty prefill", async () => {
    primeSSE(api)
    await act(async () => {
      render(
        <ChatSessionsSidebar
          initialSessions={[
            makeItem({ session_id: "deadbeefcafef00d" }),
          ]}
        />,
      )
    })
    act(() => {
      screen.getByTestId("chat-session-rename-deadbeefcafef00d").click()
    })
    const input = screen.getByTestId(
      "chat-session-rename-input-deadbeefcafef00d",
    ) as HTMLInputElement
    // Hash is not a title the operator would want to edit — start empty.
    expect(input.defaultValue).toBe("")
  })

  it("submitting non-empty title calls renameChatSession + flips row to user source", async () => {
    const renameMock = api.renameChatSession as ReturnType<typeof vi.fn>
    renameMock.mockResolvedValue({
      session_id: "sess-1",
      metadata: { auto_title: "LLM label", user_title: "Wire up deep link" },
    })
    primeSSE(api)
    await act(async () => {
      render(
        <ChatSessionsSidebar
          initialSessions={[
            makeItem({
              session_id: "sess-1",
              metadata: { auto_title: "LLM label" },
            }),
          ]}
        />,
      )
    })
    act(() => {
      screen.getByTestId("chat-session-rename-sess-1").click()
    })
    const input = screen.getByTestId(
      "chat-session-rename-input-sess-1",
    ) as HTMLInputElement
    const form = screen.getByTestId("chat-session-rename-form-sess-1")

    await act(async () => {
      fireEvent.change(input, { target: { value: "Wire up deep link" } })
      fireEvent.submit(form)
    })

    expect(renameMock).toHaveBeenCalledWith(
      "sess-1",
      "Wire up deep link",
    )
    // data-title-source flips from auto → user, auto badge disappears
    // (because the fallback chain resolves to user_title now).
    const row = screen.getByTestId("chat-session-row-sess-1")
    expect(row.getAttribute("data-title-source")).toBe("user")
    expect(
      screen.queryByTestId("chat-session-auto-badge-sess-1"),
    ).toBeNull()
    expect(
      screen.getByTestId("chat-session-title-sess-1").textContent,
    ).toBe("Wire up deep link")
  })

  it("submitting empty title calls renameChatSession(null) + reverts to auto_title", async () => {
    const renameMock = api.renameChatSession as ReturnType<typeof vi.fn>
    renameMock.mockResolvedValue({
      session_id: "sess-1",
      metadata: { auto_title: "LLM label" },
    })
    primeSSE(api)
    await act(async () => {
      render(
        <ChatSessionsSidebar
          initialSessions={[
            makeItem({
              session_id: "sess-1",
              metadata: {
                auto_title: "LLM label",
                user_title: "Temporary rename",
              },
            }),
          ]}
        />,
      )
    })
    // Sanity pre-rename: user_title wins the fallback chain.
    expect(
      screen.getByTestId("chat-session-title-sess-1").textContent,
    ).toBe("Temporary rename")

    act(() => {
      screen.getByTestId("chat-session-rename-sess-1").click()
    })
    const input = screen.getByTestId(
      "chat-session-rename-input-sess-1",
    ) as HTMLInputElement
    const form = screen.getByTestId("chat-session-rename-form-sess-1")
    await act(async () => {
      fireEvent.change(input, { target: { value: "   " } })
      fireEvent.submit(form)
    })

    // Empty/whitespace → server-side clear encoded as null.
    expect(renameMock).toHaveBeenCalledWith("sess-1", null)
    // Fallback chain drops back to auto_title — the sidebar relabels
    // without a refetch.
    const row = screen.getByTestId("chat-session-row-sess-1")
    expect(row.getAttribute("data-title-source")).toBe("auto")
    expect(
      screen.getByTestId("chat-session-title-sess-1").textContent,
    ).toBe("LLM label")
  })

  it("ESC cancels rename without calling API", async () => {
    const renameMock = api.renameChatSession as ReturnType<typeof vi.fn>
    primeSSE(api)
    await act(async () => {
      render(
        <ChatSessionsSidebar
          initialSessions={[
            makeItem({
              session_id: "sess-1",
              metadata: { auto_title: "LLM label" },
            }),
          ]}
        />,
      )
    })
    act(() => {
      screen.getByTestId("chat-session-rename-sess-1").click()
    })
    const input = screen.getByTestId("chat-session-rename-input-sess-1")
    act(() => {
      fireEvent.keyDown(input, { key: "Escape" })
    })
    expect(
      screen.queryByTestId("chat-session-rename-input-sess-1"),
    ).toBeNull()
    expect(renameMock).not.toHaveBeenCalled()
    // Row still shows the original auto_title.
    expect(
      screen.getByTestId("chat-session-title-sess-1").textContent,
    ).toBe("LLM label")
  })

  it("rollback + inline error on API failure", async () => {
    const renameMock = api.renameChatSession as ReturnType<typeof vi.fn>
    renameMock.mockRejectedValue(new Error("HTTP 500"))
    primeSSE(api)
    await act(async () => {
      render(
        <ChatSessionsSidebar
          initialSessions={[
            makeItem({
              session_id: "sess-1",
              metadata: { auto_title: "LLM label" },
            }),
          ]}
        />,
      )
    })
    act(() => {
      screen.getByTestId("chat-session-rename-sess-1").click()
    })
    const input = screen.getByTestId(
      "chat-session-rename-input-sess-1",
    ) as HTMLInputElement
    const form = screen.getByTestId("chat-session-rename-form-sess-1")
    await act(async () => {
      fireEvent.change(input, { target: { value: "Nope" } })
      fireEvent.submit(form)
    })

    // Row rolled back — the auto_title is still the effective title.
    const row = screen.getByTestId("chat-session-row-sess-1")
    expect(row.getAttribute("data-title-source")).toBe("auto")
    expect(
      screen.getByTestId("chat-session-title-sess-1").textContent,
    ).toBe("LLM label")
    // Inline error surface on the failing row.
    expect(
      screen.getByTestId("chat-session-rename-error-sess-1").textContent,
    ).toMatch(/HTTP 500/)
  })

  it("session.titled source=user relabels row in-place", async () => {
    primeSSE(api)
    const sse = primeSSE(api)
    await act(async () => {
      render(
        <ChatSessionsSidebar
          initialSessions={[
            makeItem({
              session_id: "sess-1",
              metadata: { auto_title: "LLM label" },
            }),
          ]}
        />,
      )
    })
    act(() => {
      sse.emit({
        event: "session.titled",
        data: {
          session_id: "sess-1",
          user_id: "u-1",
          title: "Renamed on device B",
          source: "user",
        },
      })
    })
    const row = screen.getByTestId("chat-session-row-sess-1")
    // Source flips to user — the ✨ auto badge is suppressed.
    expect(row.getAttribute("data-title-source")).toBe("user")
    expect(
      screen.queryByTestId("chat-session-auto-badge-sess-1"),
    ).toBeNull()
    expect(
      screen.getByTestId("chat-session-title-sess-1").textContent,
    ).toBe("Renamed on device B")
  })
})
