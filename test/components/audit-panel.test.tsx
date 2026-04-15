import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

vi.mock("@/lib/api", () => ({
  listAuditEntries: vi.fn(),
  listSessions: vi.fn(),
  getCurrentSessionId: vi.fn().mockReturnValue("full-token-abc"),
  getSSEFilterMode: vi.fn().mockReturnValue("this_session"),
  onFilterModeChange: vi.fn().mockReturnValue(() => {}),
}))

vi.mock("@/lib/auth-context", () => ({
  useAuth: vi.fn().mockReturnValue({ sessionId: "full-token-abc" }),
}))

import { AuditPanel } from "@/components/omnisight/audit-panel"
import * as api from "@/lib/api"

const MOCK_ENTRIES: api.AuditEntry[] = [
  {
    id: 1, ts: Date.now() / 1000 - 120, actor: "admin@test.com",
    action: "set_mode", entity_kind: "operation_mode", entity_id: "global",
    before: { mode: "supervised" }, after: { mode: "full_auto" },
    prev_hash: "aaa", curr_hash: "bbb",
    session_id: "full-token-abc", session_ip: "192.168.1.10", session_ua: "Chrome/120 Linux",
  },
  {
    id: 2, ts: Date.now() / 1000 - 60, actor: "admin@test.com",
    action: "resolve", entity_kind: "decision", entity_id: "dec-1",
    before: {}, after: { resolution: "approve" },
    prev_hash: "bbb", curr_hash: "ccc",
    session_id: "full-token-def", session_ip: "10.0.0.5", session_ua: "Firefox/121 Windows",
  },
]

const MOCK_SESSIONS: api.SessionItem[] = [
  {
    token_hint: "full***kabc",
    created_at: Date.now() / 1000 - 3600,
    expires_at: Date.now() / 1000 + 25200,
    last_seen_at: Date.now() / 1000 - 30,
    ip: "192.168.1.10",
    user_agent: "Chrome/120 Linux",
    is_current: true,
  },
  {
    token_hint: "full***kdef",
    created_at: Date.now() / 1000 - 7200,
    expires_at: Date.now() / 1000 + 21600,
    last_seen_at: Date.now() / 1000 - 600,
    ip: "10.0.0.5",
    user_agent: "Firefox/121 Windows",
    is_current: false,
  },
]

describe("AuditPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(api.listAuditEntries as ReturnType<typeof vi.fn>).mockResolvedValue({
      items: MOCK_ENTRIES, count: 2, filtered_to_self: false,
    })
    ;(api.listSessions as ReturnType<typeof vi.fn>).mockResolvedValue({
      items: MOCK_SESSIONS, count: 2,
    })
  })

  it("renders audit entries with actions and IPs", async () => {
    render(<AuditPanel />)
    await waitFor(() => {
      expect(screen.getByText("set_mode")).toBeInTheDocument()
      expect(screen.getByText("resolve")).toBeInTheDocument()
    })
    expect(screen.getByText("192.168.1.10")).toBeInTheDocument()
    expect(screen.getByText("10.0.0.5")).toBeInTheDocument()
  })

  it("renders session filter buttons", async () => {
    render(<AuditPanel />)
    await waitFor(() => {
      expect(screen.getByText("All Sessions")).toBeInTheDocument()
      expect(screen.getByText("Current Session")).toBeInTheDocument()
    })
  })

  it("filters by current session when clicking shortcut button", async () => {
    const user = userEvent.setup()
    render(<AuditPanel />)
    await waitFor(() => expect(screen.getByText("Current Session")).toBeInTheDocument())

    await user.click(screen.getByText("Current Session"))

    await waitFor(() => {
      expect(api.listAuditEntries).toHaveBeenCalledWith(
        expect.objectContaining({ session_id: "full-token-abc" }),
      )
    })
  })

  it("shows empty state when no entries", async () => {
    ;(api.listAuditEntries as ReturnType<typeof vi.fn>).mockResolvedValue({
      items: [], count: 0, filtered_to_self: false,
    })
    render(<AuditPanel />)
    await waitFor(() => expect(screen.getByText("No audit entries found.")).toBeInTheDocument())
  })

  it("shows device info from session_ua", async () => {
    render(<AuditPanel />)
    await waitFor(() => {
      expect(screen.getByText("Chrome on Linux")).toBeInTheDocument()
    })
  })
})
