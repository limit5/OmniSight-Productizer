/**
 * ZZ.B2 #304-2 checkbox 1 — ChatSessionsSidebar tests.
 *
 * Locks the contract of the new left-sidebar workflow/chat list:
 *  - `fetchChatSessions` is hit on mount and populates the list.
 *  - `session.titled` SSE relabels the matching row in-place without
 *    a refetch.
 *  - Fallback chain: `user_title` → `auto_title` → hash.
 *  - A ✨ auto-titled badge appears only when the source is "auto".
 *  - `chat.message` SSE bumps the matching row's recency so it
 *    floats to the top of the list.
 *  - Unknown-session `session.titled` injects a stub row so the
 *    title shows up immediately.
 */

import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, screen, act } from "@testing-library/react"

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>()
  return {
    ...actual,
    subscribeEvents: vi.fn(),
    fetchChatSessions: vi.fn(),
  }
})

import * as api from "@/lib/api"
import {
  ChatSessionsSidebar,
  resolveSessionTitle,
} from "@/components/omnisight/chat-sessions-sidebar"
import { primeSSE } from "../helpers/sse"

beforeEach(() => {
  vi.clearAllMocks()
})

function makeItem(
  overrides: Partial<api.ChatSessionItem> = {},
): api.ChatSessionItem {
  return {
    session_id: "sess-abc123def456",
    user_id: "u-1",
    tenant_id: "t-default",
    metadata: {},
    created_at: 100,
    updated_at: 100,
    ...overrides,
  }
}

describe("resolveSessionTitle fallback chain", () => {
  it("picks user_title first when set", () => {
    const r = resolveSessionTitle(
      makeItem({
        metadata: { user_title: "My rename", auto_title: "LLM title" },
      }),
    )
    expect(r.title).toBe("My rename")
    expect(r.source).toBe("user")
  })

  it("picks auto_title when only auto_title is present", () => {
    const r = resolveSessionTitle(
      makeItem({ metadata: { auto_title: "LLM title" } }),
    )
    expect(r.title).toBe("LLM title")
    expect(r.source).toBe("auto")
  })

  it("falls back to hash when neither title is present", () => {
    const r = resolveSessionTitle(
      makeItem({ session_id: "deadbeefcafef00d" }),
    )
    expect(r.title).toBe("deadbeef…")
    expect(r.source).toBe("hash")
  })

  it("treats empty/whitespace titles as absent", () => {
    const r = resolveSessionTitle(
      makeItem({
        metadata: { user_title: "   ", auto_title: "" },
        session_id: "ffeeddccbb",
      }),
    )
    expect(r.source).toBe("hash")
  })
})

describe("ChatSessionsSidebar", () => {
  it("renders initial items via fetchChatSessions on mount", async () => {
    ;(api.fetchChatSessions as ReturnType<typeof vi.fn>).mockResolvedValue({
      items: [
        makeItem({ session_id: "aaaaaaaabbbb", metadata: { auto_title: "First chat" } }),
        makeItem({ session_id: "ccccccccdddd", updated_at: 50 }),
      ],
      count: 2,
    })
    primeSSE(api)

    await act(async () => {
      render(<ChatSessionsSidebar />)
    })

    expect(api.fetchChatSessions).toHaveBeenCalledWith({ limit: 50 })
    expect(
      screen.getByTestId("chat-session-title-aaaaaaaabbbb").textContent,
    ).toBe("First chat")
    // Hash fallback renders the short hash.
    expect(
      screen.getByTestId("chat-session-title-ccccccccdddd").textContent,
    ).toBe("cccccccc…")
  })

  it("relabels row on session.titled SSE without a refetch", async () => {
    ;(api.fetchChatSessions as ReturnType<typeof vi.fn>).mockResolvedValue({
      items: [makeItem({ session_id: "sess-aaa" })],
      count: 1,
    })
    const sse = primeSSE(api)

    await act(async () => {
      render(<ChatSessionsSidebar />)
    })

    // Pre-SSE: hash fallback.
    expect(
      screen.getByTestId("chat-session-title-sess-aaa").textContent,
    ).toBe("sess-aaa…")
    // Badge not present yet — source is "hash".
    expect(
      screen.queryByTestId("chat-session-auto-badge-sess-aaa"),
    ).toBeNull()

    act(() => {
      sse.emit({
        event: "session.titled",
        data: {
          session_id: "sess-aaa",
          user_id: "u-1",
          title: "Wire up deep link",
          source: "auto",
        },
      })
    })

    expect(
      screen.getByTestId("chat-session-title-sess-aaa").textContent,
    ).toBe("Wire up deep link")
    // Auto-titled badge appears only for source="auto".
    expect(
      screen.getByTestId("chat-session-auto-badge-sess-aaa"),
    ).toBeTruthy()
    // Refetch was NOT triggered.
    expect(api.fetchChatSessions).toHaveBeenCalledTimes(1)
  })

  it("injects a stub row when session.titled arrives for unknown session", async () => {
    ;(api.fetchChatSessions as ReturnType<typeof vi.fn>).mockResolvedValue({
      items: [],
      count: 0,
    })
    const sse = primeSSE(api)

    await act(async () => {
      render(<ChatSessionsSidebar />)
    })

    expect(screen.getByTestId("chat-sessions-empty")).toBeTruthy()

    act(() => {
      sse.emit({
        event: "session.titled",
        data: {
          session_id: "sess-new",
          user_id: "u-1",
          title: "Ghost chat title",
          source: "auto",
        },
      })
    })

    expect(
      screen.getByTestId("chat-session-title-sess-new").textContent,
    ).toBe("Ghost chat title")
  })

  it("bumps recency on chat.message SSE so row floats to top", async () => {
    ;(api.fetchChatSessions as ReturnType<typeof vi.fn>).mockResolvedValue({
      items: [
        makeItem({ session_id: "sess-new-top", updated_at: 500 }),
        makeItem({ session_id: "sess-stale", updated_at: 100 }),
      ],
      count: 2,
    })
    const sse = primeSSE(api)

    await act(async () => {
      render(<ChatSessionsSidebar />)
    })

    // Initial order: new-top first.
    const initialRows = screen.getAllByTestId(/^chat-session-row-/)
    expect(initialRows[0].getAttribute("data-testid")).toBe(
      "chat-session-row-sess-new-top",
    )

    act(() => {
      sse.emit({
        event: "chat.message",
        data: {
          id: "msg-1",
          user_id: "u-1",
          role: "user",
          content: "new turn",
          ts: "2026-04-24T00:00:00",
          session_id: "sess-stale",
          timestamp: "2026-04-24T00:00:00",
        },
      })
    })

    // After the bump, sess-stale floats above sess-new-top.
    const reordered = screen.getAllByTestId(/^chat-session-row-/)
    expect(reordered[0].getAttribute("data-testid")).toBe(
      "chat-session-row-sess-stale",
    )
  })

  it("prefers user_title over auto_title when both are present", async () => {
    ;(api.fetchChatSessions as ReturnType<typeof vi.fn>).mockResolvedValue({
      items: [
        makeItem({
          session_id: "sess-dual",
          metadata: { user_title: "Renamed", auto_title: "LLM" },
        }),
      ],
      count: 1,
    })
    primeSSE(api)

    await act(async () => {
      render(<ChatSessionsSidebar />)
    })

    expect(
      screen.getByTestId("chat-session-title-sess-dual").textContent,
    ).toBe("Renamed")
    // data-title-source is "user" so the ✨ auto badge is NOT rendered.
    const row = screen.getByTestId("chat-session-row-sess-dual")
    expect(row.getAttribute("data-title-source")).toBe("user")
    expect(
      screen.queryByTestId("chat-session-auto-badge-sess-dual"),
    ).toBeNull()
  })

  it("onSelect fires with the session_id when operator clicks a row", async () => {
    ;(api.fetchChatSessions as ReturnType<typeof vi.fn>).mockResolvedValue({
      items: [makeItem({ session_id: "sess-click" })],
      count: 1,
    })
    primeSSE(api)
    const onSelect = vi.fn()

    await act(async () => {
      render(<ChatSessionsSidebar onSelect={onSelect} />)
    })

    act(() => {
      screen.getByTestId("chat-session-row-sess-click")
        .querySelector("button")!
        .click()
    })

    expect(onSelect).toHaveBeenCalledWith("sess-click")
  })

  it("does not fetch when initialSessions is provided", async () => {
    primeSSE(api)
    await act(async () => {
      render(
        <ChatSessionsSidebar
          initialSessions={[
            makeItem({
              session_id: "sess-seed",
              metadata: { auto_title: "Seeded" },
            }),
          ]}
        />,
      )
    })
    expect(api.fetchChatSessions).not.toHaveBeenCalled()
    expect(
      screen.getByTestId("chat-session-title-sess-seed").textContent,
    ).toBe("Seeded")
  })
})
