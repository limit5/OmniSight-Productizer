/**
 * OP-735 R5 -- /admin/batch-merge operator dashboard contract.
 *
 * Locks in:
 *   1. Auth gate -- non-admin sees 403 placeholder, no list call.
 *   2. Happy-path list render with select-all + per-row checkbox state.
 *   3. Filter inputs trigger refresh with the right query params.
 *   4. Bulk +2 button is disabled while nothing is selected, then
 *      calls approveBatchMergeCandidates with the selected ids and
 *      refreshes the list afterwards.
 *   5. Per-row failure surface (rejected by Gerrit etc.) renders inline.
 */

import React from "react"
import { describe, expect, it, vi, beforeEach } from "vitest"
import {
  render,
  screen,
  fireEvent,
  waitFor,
  cleanup,
} from "@testing-library/react"

vi.mock("@/lib/auth-context", () => ({
  useAuth: vi.fn(),
}))

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>()
  return {
    ...actual,
    listBatchMergeCandidates: vi.fn(),
    approveBatchMergeCandidates: vi.fn(),
  }
})

import AdminBatchMergePage from "@/components/admin/batch-merge-page"
import { useAuth } from "@/lib/auth-context"
import {
  listBatchMergeCandidates,
  approveBatchMergeCandidates,
  type BatchMergeCandidateRow,
} from "@/lib/api"

const mockedUseAuth = useAuth as unknown as ReturnType<typeof vi.fn>
const mockedList = listBatchMergeCandidates as unknown as ReturnType<typeof vi.fn>
const mockedApprove = approveBatchMergeCandidates as unknown as ReturnType<
  typeof vi.fn
>

const sampleRows: BatchMergeCandidateRow[] = [
  {
    change_id: "I001",
    project: "omnisight",
    bot: "claude-bot",
    file_class: "docs",
    insertions: 30,
    deletions: 2,
    files: ["docs/howto.md"],
    ai_summary: "LGTM",
    tagged_at: Math.floor(Date.now() / 1000) - 60,
    revision: "abc",
    subject: "[OP-100] doc tweak",
    agent_class: "subscription-claude",
    tier: "S",
  },
  {
    change_id: "I002",
    project: "omnisight",
    bot: "claude-bot",
    file_class: "backend-agents",
    insertions: 20,
    deletions: 5,
    files: ["backend/agents/foo.py"],
    ai_summary: "rename only",
    tagged_at: Math.floor(Date.now() / 1000) - 120,
    revision: "def",
    subject: "[OP-101] rename helper",
    agent_class: "subscription-claude",
    tier: "M",
  },
]

beforeEach(() => {
  vi.clearAllMocks()
  cleanup()
})


describe("/admin/batch-merge -- access gate", () => {
  it("renders the 403 placeholder when caller is not admin+", () => {
    mockedUseAuth.mockReturnValue({
      user: {
        id: "u-1",
        email: "viewer@x.io",
        name: "V",
        role: "viewer",
        enabled: true,
        tenant_id: "t-default",
      },
      authMode: "session",
      loading: false,
    })

    render(<AdminBatchMergePage />)
    expect(screen.getByTestId("batch-merge-forbidden")).toBeInTheDocument()
    expect(mockedList).not.toHaveBeenCalled()
  })

  it("treats authMode=open (dev anon admin) as admin", async () => {
    mockedUseAuth.mockReturnValue({
      user: null,
      authMode: "open",
      loading: false,
    })
    mockedList.mockResolvedValue({
      candidates: [],
      hashtag: "runner-batch-merge-candidate",
      operator: "anon@open",
      fetched_at: Date.now() / 1000,
    })

    render(<AdminBatchMergePage />)
    await waitFor(() =>
      expect(screen.getByTestId("batch-merge-page")).toBeInTheDocument(),
    )
    expect(mockedList).toHaveBeenCalledTimes(1)
  })
})


describe("/admin/batch-merge -- happy path list + bulk +2", () => {
  beforeEach(() => {
    mockedUseAuth.mockReturnValue({
      user: {
        id: "u-admin",
        email: "admin@x.io",
        name: "Admin",
        role: "admin",
        enabled: true,
        tenant_id: "t-default",
      },
      authMode: "session",
      loading: false,
    })
  })

  it("renders rows and exposes select-all + per-row checkboxes", async () => {
    mockedList.mockResolvedValue({
      candidates: sampleRows,
      hashtag: "runner-batch-merge-candidate",
      operator: "admin@x.io",
      fetched_at: Date.now() / 1000,
    })

    render(<AdminBatchMergePage />)
    await waitFor(() =>
      expect(screen.getByTestId("batch-merge-row-I001")).toBeInTheDocument(),
    )
    expect(screen.getByTestId("batch-merge-row-I002")).toBeInTheDocument()

    // Approve button starts disabled -- nothing selected.
    const approveBtn = screen.getByTestId("batch-merge-approve")
    expect(approveBtn).toBeDisabled()

    // Click select-all
    fireEvent.click(screen.getByTestId("batch-merge-select-all"))
    expect(screen.getByTestId("batch-merge-selected-count")).toHaveTextContent(
      "2 selected",
    )
    expect(approveBtn).toBeEnabled()
  })

  it("calls approveBatchMergeCandidates with the selected ids and refreshes", async () => {
    mockedList.mockResolvedValueOnce({
      candidates: sampleRows,
      hashtag: "runner-batch-merge-candidate",
      operator: "admin@x.io",
      fetched_at: Date.now() / 1000,
    })
    mockedApprove.mockResolvedValue({
      results: [
        { change_id: "I001", ok: true, reason: "" },
        { change_id: "I002", ok: true, reason: "" },
      ],
      succeeded: 2,
      failed: 0,
      operator: "admin@x.io",
    })
    mockedList.mockResolvedValueOnce({
      candidates: [],
      hashtag: "runner-batch-merge-candidate",
      operator: "admin@x.io",
      fetched_at: Date.now() / 1000,
    })

    render(<AdminBatchMergePage />)
    await waitFor(() =>
      expect(screen.getByTestId("batch-merge-row-I001")).toBeInTheDocument(),
    )

    fireEvent.click(screen.getByTestId("batch-merge-select-I001"))
    fireEvent.click(screen.getByTestId("batch-merge-select-I002"))
    fireEvent.click(screen.getByTestId("batch-merge-approve"))

    await waitFor(() =>
      expect(mockedApprove).toHaveBeenCalledWith(["I001", "I002"]),
    )
    await waitFor(() =>
      expect(screen.getByTestId("batch-merge-bulk-summary")).toHaveTextContent(
        "2 approved, 0 failed.",
      ),
    )

    // Refresh-after-approve was triggered; the empty list response
    // means the rows have fallen off the registry.
    await waitFor(() =>
      expect(screen.getByTestId("batch-merge-empty")).toBeInTheDocument(),
    )
    expect(mockedList).toHaveBeenCalledTimes(2)
  })

  it("surfaces per-row failures inline", async () => {
    mockedList.mockResolvedValue({
      candidates: sampleRows,
      hashtag: "runner-batch-merge-candidate",
      operator: "admin@x.io",
      fetched_at: Date.now() / 1000,
    })
    mockedApprove.mockResolvedValue({
      results: [
        { change_id: "I001", ok: true, reason: "" },
        { change_id: "I002", ok: false, reason: "gerrit: permission denied" },
      ],
      succeeded: 1,
      failed: 1,
      operator: "admin@x.io",
    })

    render(<AdminBatchMergePage />)
    await waitFor(() =>
      expect(screen.getByTestId("batch-merge-row-I001")).toBeInTheDocument(),
    )

    fireEvent.click(screen.getByTestId("batch-merge-select-all"))
    fireEvent.click(screen.getByTestId("batch-merge-approve"))

    await waitFor(() =>
      expect(
        screen.getByTestId("batch-merge-result-fail-I002"),
      ).toBeInTheDocument(),
    )
    expect(screen.getByTestId("batch-merge-result-fail-I002")).toHaveTextContent(
      "permission denied",
    )
    expect(screen.getByTestId("batch-merge-bulk-summary")).toHaveTextContent(
      "1 approved, 1 failed.",
    )
  })

  it("passes filter inputs to listBatchMergeCandidates on refresh", async () => {
    mockedList.mockResolvedValue({
      candidates: sampleRows,
      hashtag: "runner-batch-merge-candidate",
      operator: "admin@x.io",
      fetched_at: Date.now() / 1000,
    })

    render(<AdminBatchMergePage />)
    await waitFor(() =>
      expect(mockedList).toHaveBeenCalledWith({
        agent_class: undefined,
        tier: undefined,
        file_glob: undefined,
      }),
    )

    fireEvent.change(screen.getByTestId("batch-merge-filter-agent-class"), {
      target: { value: "subscription-codex" },
    })
    fireEvent.change(screen.getByTestId("batch-merge-filter-tier"), {
      target: { value: "L" },
    })
    fireEvent.change(screen.getByTestId("batch-merge-filter-file-glob"), {
      target: { value: "backend/" },
    })
    fireEvent.click(screen.getByTestId("batch-merge-refresh"))

    await waitFor(() =>
      expect(mockedList).toHaveBeenLastCalledWith({
        agent_class: "subscription-codex",
        tier: "L",
        file_glob: "backend/",
      }),
    )
  })
})
