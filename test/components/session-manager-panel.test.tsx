import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

vi.mock("@/lib/api", () => ({
  listSessions: vi.fn(),
  revokeSession: vi.fn(),
  revokeAllOtherSessions: vi.fn(),
}))

import { SessionManagerPanel } from "@/components/omnisight/session-manager-panel"
import * as api from "@/lib/api"

const CURRENT_SESSION: api.SessionItem = {
  token_hint: "abc1***xyz1",
  created_at: Date.now() / 1000 - 3600,
  expires_at: Date.now() / 1000 + 25200,
  last_seen_at: Date.now() / 1000 - 30,
  ip: "192.168.1.10",
  user_agent: "Mozilla/5.0 (X11; Linux x86_64) Chrome/120.0",
  is_current: true,
}

const OTHER_SESSION: api.SessionItem = {
  token_hint: "def2***uvw2",
  created_at: Date.now() / 1000 - 7200,
  expires_at: Date.now() / 1000 + 21600,
  last_seen_at: Date.now() / 1000 - 600,
  ip: "10.0.0.5",
  user_agent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Firefox/121.0",
  is_current: false,
}

describe("SessionManagerPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it("renders sessions with current device badge", async () => {
    ;(api.listSessions as ReturnType<typeof vi.fn>).mockResolvedValue({
      items: [CURRENT_SESSION, OTHER_SESSION],
      count: 2,
    })

    render(<SessionManagerPanel />)
    await waitFor(() => expect(screen.getByTestId("this-device-badge")).toBeInTheDocument())

    expect(screen.getByText("This device")).toBeInTheDocument()
    expect(screen.getByText("Chrome on Linux")).toBeInTheDocument()
    expect(screen.getByText("Firefox on Windows")).toBeInTheDocument()
    expect(screen.getByText("192.168.1.10")).toBeInTheDocument()
    expect(screen.getByText("10.0.0.5")).toBeInTheDocument()
  })

  it("does not show revoke button for current session", async () => {
    ;(api.listSessions as ReturnType<typeof vi.fn>).mockResolvedValue({
      items: [CURRENT_SESSION],
      count: 1,
    })

    render(<SessionManagerPanel />)
    await waitFor(() => expect(screen.getByTestId("this-device-badge")).toBeInTheDocument())

    expect(screen.queryByTestId(`revoke-${CURRENT_SESSION.token_hint}`)).not.toBeInTheDocument()
  })

  it("shows revoke button for other sessions", async () => {
    ;(api.listSessions as ReturnType<typeof vi.fn>).mockResolvedValue({
      items: [CURRENT_SESSION, OTHER_SESSION],
      count: 2,
    })

    render(<SessionManagerPanel />)
    await waitFor(() => expect(screen.getByTestId(`revoke-${OTHER_SESSION.token_hint}`)).toBeInTheDocument())
  })

  it("revokes a single session on click", async () => {
    const user = userEvent.setup()
    ;(api.listSessions as ReturnType<typeof vi.fn>).mockResolvedValue({
      items: [CURRENT_SESSION, OTHER_SESSION],
      count: 2,
    })
    ;(api.revokeSession as ReturnType<typeof vi.fn>).mockResolvedValue({ status: "revoked" })

    render(<SessionManagerPanel />)
    await waitFor(() => expect(screen.getByTestId(`revoke-${OTHER_SESSION.token_hint}`)).toBeInTheDocument())

    await user.click(screen.getByTestId(`revoke-${OTHER_SESSION.token_hint}`))

    await waitFor(() => {
      expect(api.revokeSession).toHaveBeenCalledWith(OTHER_SESSION.token_hint)
      expect(screen.queryByTestId(`session-row-${OTHER_SESSION.token_hint}`)).not.toBeInTheDocument()
    })
  })

  it("revokes all other sessions", async () => {
    const user = userEvent.setup()
    ;(api.listSessions as ReturnType<typeof vi.fn>).mockResolvedValue({
      items: [CURRENT_SESSION, OTHER_SESSION],
      count: 2,
    })
    ;(api.revokeAllOtherSessions as ReturnType<typeof vi.fn>).mockResolvedValue({
      status: "revoked",
      revoked_count: 1,
    })

    render(<SessionManagerPanel />)
    await waitFor(() => expect(screen.getByTestId("revoke-all-others")).toBeInTheDocument())

    await user.click(screen.getByTestId("revoke-all-others"))

    await waitFor(() => {
      expect(api.revokeAllOtherSessions).toHaveBeenCalled()
      expect(screen.queryByTestId(`session-row-${OTHER_SESSION.token_hint}`)).not.toBeInTheDocument()
      expect(screen.getByTestId(`session-row-${CURRENT_SESSION.token_hint}`)).toBeInTheDocument()
    })
  })

  it("shows loading state", async () => {
    let resolve: (v: { items: api.SessionItem[]; count: number }) => void
    ;(api.listSessions as ReturnType<typeof vi.fn>).mockReturnValue(
      new Promise(r => { resolve = r })
    )

    render(<SessionManagerPanel />)
    expect(screen.getByText("Loading sessions...")).toBeInTheDocument()

    resolve!({ items: [CURRENT_SESSION], count: 1 })
    await waitFor(() => expect(screen.queryByText("Loading sessions...")).not.toBeInTheDocument())
  })

  it("shows error on fetch failure", async () => {
    ;(api.listSessions as ReturnType<typeof vi.fn>).mockRejectedValue(new Error("network error"))

    render(<SessionManagerPanel />)
    await waitFor(() => expect(screen.getByText("network error")).toBeInTheDocument())
  })

  it("hides revoke-all-others when no other sessions exist", async () => {
    ;(api.listSessions as ReturnType<typeof vi.fn>).mockResolvedValue({
      items: [CURRENT_SESSION],
      count: 1,
    })

    render(<SessionManagerPanel />)
    await waitFor(() => expect(screen.getByTestId("this-device-badge")).toBeInTheDocument())
    expect(screen.queryByTestId("revoke-all-others")).not.toBeInTheDocument()
  })
})
