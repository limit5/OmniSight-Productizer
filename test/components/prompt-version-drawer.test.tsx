/**
 * ZZ.C1 (#305-1, 2026-04-24) checkbox 3 — PromptVersionDrawer test matrix.
 *
 * Locks the agent-selector + timeline + side-by-side diff contract for the
 * drawer that opens from the ORCHESTRATOR AI panel:
 *   - 12 subtypes available in the dropdown (per row spec)
 *   - timeline rows surface created_at + 8-char hash + 2-line preview
 *   - clicking two rows renders the side-by-side diff with red/green
 *     row backgrounds
 *   - changing the agent_type wipes the prior selection (cross-agent
 *     diff is invalid; the backend rejects it with 400)
 *   - identical-hash and total-rewrite edge cases collapse correctly
 *     (this is the UI half of TODO checkbox 5's "diff render edge case"
 *     coverage; the backend half landed under checkbox 1)
 *
 * Tests drive the component via the ``fetchVersions`` test seam so we
 * don't have to mock the @/lib/api module surface.
 */

import { describe, expect, it, vi } from "vitest"
import { render, fireEvent, act, waitFor } from "@testing-library/react"

import {
  PromptVersionDrawer,
  changesToDiffRows,
  DEFAULT_PROMPT_AGENT_SUBTYPES,
  type PromptAgentSubtype,
} from "@/components/omnisight/prompt-version-drawer"
import type {
  PromptVersionEntry,
  PromptVersionsListResponse,
} from "@/lib/api"
import type { Change } from "diff"

function makeEntry(over: Partial<PromptVersionEntry> = {}): PromptVersionEntry {
  return {
    id: 1,
    agent_type: "orchestrator",
    content_hash: "0".repeat(64),
    content: "line one\nline two\nline three\n",
    content_preview: "line one\nline two",
    created_at: "2026-04-24T08:15:00Z",
    supersedes_id: null,
    version: 1,
    role: "active",
    ...over,
  }
}

function makeListResponse(
  versions: PromptVersionEntry[],
  agentType = "orchestrator",
): PromptVersionsListResponse {
  return {
    agent_type: agentType,
    path: `backend/agents/prompts/${agentType}.md`,
    limit: 20,
    versions,
  }
}

describe("DEFAULT_PROMPT_AGENT_SUBTYPES", () => {
  it("ships the 12 subtypes the row spec mandates", () => {
    expect(DEFAULT_PROMPT_AGENT_SUBTYPES).toHaveLength(12)
    const slugs = DEFAULT_PROMPT_AGENT_SUBTYPES.map(s => s.value)
    expect(slugs).toContain("orchestrator")
    expect(slugs).toContain("firmware")
    expect(slugs).toContain("software")
    expect(slugs).toContain("validator")
    // All slugs match the [A-Za-z0-9_-]+ fence the backend enforces.
    for (const s of slugs) {
      expect(s).toMatch(/^[A-Za-z0-9_-]+$/)
    }
  })
})

describe("changesToDiffRows()", () => {
  it("renders identical bodies as all-context rows", () => {
    const changes: Change[] = [
      { value: "a\nb\nc\n", added: false, removed: false, count: 3 },
    ]
    const rows = changesToDiffRows(changes)
    expect(rows).toHaveLength(3)
    expect(rows.every(r => r.kind === "context")).toBe(true)
    expect(rows[0]).toEqual({ kind: "context", left: "a", right: "a" })
  })

  it("renders empty/empty as zero rows", () => {
    expect(changesToDiffRows([])).toEqual([])
  })

  it("renders a total rewrite as red-then-green with no context", () => {
    // diffLines emits two changes in order: removed block, then added.
    const changes: Change[] = [
      { value: "old1\nold2\n", added: false, removed: true, count: 2 },
      { value: "new1\nnew2\nnew3\n", added: true, removed: false, count: 3 },
    ]
    const rows = changesToDiffRows(changes)
    // Edit zip: 2 removed lines + 3 added lines → 2 zip pairs (each
    // exploded into a removed row + an added row, total 4) + 1 trailing
    // added-only row = 5 rows.
    expect(rows).toHaveLength(5)
    expect(rows.filter(r => r.kind === "removed")).toHaveLength(2)
    expect(rows.filter(r => r.kind === "added")).toHaveLength(3)
    expect(rows.filter(r => r.kind === "context")).toHaveLength(0)
  })

  it("handles a pure-deletion (no following addition)", () => {
    const changes: Change[] = [
      { value: "kept\n", added: false, removed: false, count: 1 },
      { value: "gone\n", added: false, removed: true, count: 1 },
    ]
    const rows = changesToDiffRows(changes)
    expect(rows).toEqual([
      { kind: "context", left: "kept", right: "kept" },
      { kind: "removed", left: "gone", right: null },
    ])
  })

  it("handles a pure-addition (no preceding removal)", () => {
    const changes: Change[] = [
      { value: "kept\n", added: false, removed: false, count: 1 },
      { value: "fresh\n", added: true, removed: false, count: 1 },
    ]
    const rows = changesToDiffRows(changes)
    expect(rows).toEqual([
      { kind: "context", left: "kept", right: "kept" },
      { kind: "added", left: null, right: "fresh" },
    ])
  })
})

describe("<PromptVersionDrawer>", () => {
  it("does not render when open=false", () => {
    const fetcher = vi.fn().mockResolvedValue(makeListResponse([]))
    const view = render(
      <PromptVersionDrawer open={false} onClose={() => {}} fetchVersions={fetcher} />,
    )
    expect(view.queryByTestId("prompt-version-drawer")).toBeNull()
    expect(fetcher).not.toHaveBeenCalled()
  })

  it("renders a 12-option agent selector + fetches the default agent on open", async () => {
    const fetcher = vi.fn().mockResolvedValue(makeListResponse([makeEntry()]))
    const view = render(
      <PromptVersionDrawer open={true} onClose={() => {}} fetchVersions={fetcher} />,
    )
    expect(view.getByTestId("prompt-version-drawer")).toBeInTheDocument()
    const select = view.getByTestId("prompt-version-drawer-agent-select") as HTMLSelectElement
    // 12 options total inside the optgroup tree.
    expect(select.querySelectorAll("option")).toHaveLength(12)
    await waitFor(() => expect(fetcher).toHaveBeenCalledWith("orchestrator", 20))
  })

  it("renders the empty state when the API returns zero rows", async () => {
    const fetcher = vi.fn().mockResolvedValue(makeListResponse([]))
    const view = render(
      <PromptVersionDrawer open={true} onClose={() => {}} fetchVersions={fetcher} />,
    )
    await waitFor(() =>
      expect(view.getByTestId("prompt-version-drawer-empty")).toBeInTheDocument(),
    )
  })

  it("surfaces the fetch error in the timeline body", async () => {
    const fetcher = vi.fn().mockRejectedValue(new Error("503 boom"))
    const view = render(
      <PromptVersionDrawer open={true} onClose={() => {}} fetchVersions={fetcher} />,
    )
    await waitFor(() => {
      const err = view.getByTestId("prompt-version-drawer-error")
      expect(err).toBeInTheDocument()
      expect(err).toHaveTextContent(/503 boom/)
    })
  })

  it("renders timeline rows with hash prefix + created_at + 2-line preview", async () => {
    const fetcher = vi.fn().mockResolvedValue(
      makeListResponse([
        makeEntry({
          id: 7,
          version: 7,
          content_hash: "abcdef0123456789".padEnd(64, "0"),
          created_at: "2026-04-24T09:42:11Z",
          content_preview: "first line of the prompt\nsecond line of the prompt",
        }),
      ]),
    )
    const view = render(
      <PromptVersionDrawer open={true} onClose={() => {}} fetchVersions={fetcher} />,
    )
    await waitFor(() => view.getByTestId("prompt-version-row"))
    const hash = view.getByTestId("prompt-version-hash")
    expect(hash).toHaveTextContent("abcdef01")
    const ts = view.getByTestId("prompt-version-created-at")
    expect(ts).toHaveTextContent("04-24 09:42")
    const prev = view.getByTestId("prompt-version-preview")
    expect(prev).toHaveTextContent(/first line of the prompt/)
    expect(prev).toHaveTextContent(/second line of the prompt/)
  })

  it("renders side-by-side diff after picking two rows", async () => {
    const v1 = makeEntry({
      id: 1,
      version: 1,
      content_hash: "a".repeat(64),
      content: "line one\nline two\nline three\n",
    })
    const v2 = makeEntry({
      id: 2,
      version: 2,
      content_hash: "b".repeat(64),
      content: "line one\nline TWO\nline three\nadded line\n",
    })
    const fetcher = vi.fn().mockResolvedValue(makeListResponse([v2, v1]))
    const view = render(
      <PromptVersionDrawer open={true} onClose={() => {}} fetchVersions={fetcher} />,
    )
    await waitFor(() => expect(view.getAllByTestId("prompt-version-row")).toHaveLength(2))
    // Single selection → diff-prompt placeholder still visible.
    expect(view.getByTestId("prompt-version-drawer-diff-prompt")).toBeInTheDocument()
    const rows = view.getAllByTestId("prompt-version-row")
    act(() => { fireEvent.click(rows[0]) })
    expect(view.getByTestId("prompt-version-drawer-diff-prompt")).toBeInTheDocument()
    act(() => { fireEvent.click(rows[1]) })
    // Two selections → side-by-side appears.
    const sbs = await waitFor(() => view.getByTestId("prompt-version-diff-side-by-side"))
    expect(sbs).toBeInTheDocument()
    // ``v1.version=1 < v2.version=2`` → FROM=v1, TO=v2 regardless of pick order.
    const summary = view.getByTestId("prompt-version-diff-summary")
    // At least one removed line (line two → line TWO) + 2 added (TWO + added line).
    expect(summary.textContent).toMatch(/[+]\d/)
    expect(summary.textContent).toMatch(/−\d/)
    // Diff rows of both kinds rendered in the table.
    const diffRows = view.getAllByTestId("prompt-version-diff-row")
    const kinds = new Set(diffRows.map(r => r.getAttribute("data-row-kind")))
    expect(kinds.has("removed")).toBe(true)
    expect(kinds.has("added")).toBe(true)
  })

  it("collapses to the identical-hash short-circuit when both picks share content_hash", async () => {
    const sameHash = "c".repeat(64)
    const v1 = makeEntry({ id: 10, version: 5, content_hash: sameHash, content: "same body\n" })
    // Same hash but different ids — possible if the dedupe race lost
    // (two writers committed the same hash with different version
    // numbers before the fast-path SELECT could short-circuit).
    const v2 = makeEntry({ id: 11, version: 6, content_hash: sameHash, content: "same body\n" })
    const fetcher = vi.fn().mockResolvedValue(makeListResponse([v2, v1]))
    const view = render(
      <PromptVersionDrawer open={true} onClose={() => {}} fetchVersions={fetcher} />,
    )
    await waitFor(() => expect(view.getAllByTestId("prompt-version-row")).toHaveLength(2))
    const rows = view.getAllByTestId("prompt-version-row")
    act(() => { fireEvent.click(rows[0]) })
    act(() => { fireEvent.click(rows[1]) })
    expect(view.getByTestId("prompt-version-diff-identical")).toBeInTheDocument()
    expect(view.queryByTestId("prompt-version-diff-side-by-side")).toBeNull()
  })

  it("renders the empty-diff state when both bodies are empty strings", async () => {
    const v1 = makeEntry({ id: 1, version: 1, content_hash: "d".repeat(64), content: "" })
    const v2 = makeEntry({ id: 2, version: 2, content_hash: "e".repeat(64), content: "" })
    const fetcher = vi.fn().mockResolvedValue(makeListResponse([v2, v1]))
    const view = render(
      <PromptVersionDrawer open={true} onClose={() => {}} fetchVersions={fetcher} />,
    )
    await waitFor(() => expect(view.getAllByTestId("prompt-version-row")).toHaveLength(2))
    const rows = view.getAllByTestId("prompt-version-row")
    act(() => { fireEvent.click(rows[0]) })
    act(() => { fireEvent.click(rows[1]) })
    expect(view.getByTestId("prompt-version-diff-empty")).toBeInTheDocument()
  })

  it("wipes the selection when the operator switches agent_type", async () => {
    const v1 = makeEntry({ id: 1, version: 1, content_hash: "a".repeat(64), content: "x\n" })
    const v2 = makeEntry({ id: 2, version: 2, content_hash: "b".repeat(64), content: "y\n" })
    const fetcher = vi.fn().mockImplementation(async (agent: string) =>
      makeListResponse(agent === "orchestrator" ? [v2, v1] : [], agent),
    )
    const view = render(
      <PromptVersionDrawer open={true} onClose={() => {}} fetchVersions={fetcher} />,
    )
    await waitFor(() => expect(view.getAllByTestId("prompt-version-row")).toHaveLength(2))
    const rows = view.getAllByTestId("prompt-version-row")
    act(() => { fireEvent.click(rows[0]) })
    act(() => { fireEvent.click(rows[1]) })
    expect(view.getByTestId("prompt-version-diff-side-by-side")).toBeInTheDocument()
    // Switch agent — selection wipes, diff disappears.
    const select = view.getByTestId("prompt-version-drawer-agent-select")
    act(() => { fireEvent.change(select, { target: { value: "firmware" } }) })
    await waitFor(() => expect(fetcher).toHaveBeenCalledWith("firmware", 20))
    await waitFor(() => expect(view.getByTestId("prompt-version-drawer-empty")).toBeInTheDocument())
    expect(view.queryByTestId("prompt-version-diff-side-by-side")).toBeNull()
    expect(view.getByTestId("prompt-version-drawer-diff-prompt")).toBeInTheDocument()
  })

  it("dedupe-by-hash row spec: same hash three times collapses to one timeline entry", async () => {
    // ZZ.C1 checkbox 5 (UI half of the "hash 去重" requirement) — the
    // backend already dedupes by content_hash, so the drawer just needs
    // to render whatever the API returns. We assert on the shape we
    // expect by giving the fetcher a single deduped row even though the
    // underlying scenario was 3 same-hash inserts.
    const fetcher = vi.fn().mockResolvedValue(
      makeListResponse([
        makeEntry({ id: 30, version: 30, content_hash: "f".repeat(64) }),
      ]),
    )
    const view = render(
      <PromptVersionDrawer open={true} onClose={() => {}} fetchVersions={fetcher} />,
    )
    await waitFor(() => expect(view.getAllByTestId("prompt-version-row")).toHaveLength(1))
  })

  it("third pick drops the oldest selection (FIFO)", async () => {
    const v1 = makeEntry({ id: 1, version: 1, content_hash: "a".repeat(64), content: "a\n" })
    const v2 = makeEntry({ id: 2, version: 2, content_hash: "b".repeat(64), content: "b\n" })
    const v3 = makeEntry({ id: 3, version: 3, content_hash: "c".repeat(64), content: "c\n" })
    const fetcher = vi.fn().mockResolvedValue(makeListResponse([v3, v2, v1]))
    const view = render(
      <PromptVersionDrawer open={true} onClose={() => {}} fetchVersions={fetcher} />,
    )
    await waitFor(() => expect(view.getAllByTestId("prompt-version-row")).toHaveLength(3))
    const rows = view.getAllByTestId("prompt-version-row")
    // rows order matches versions array (DESC): [v3, v2, v1]
    act(() => { fireEvent.click(rows[2]) }) // v1
    act(() => { fireEvent.click(rows[1]) }) // v2
    act(() => { fireEvent.click(rows[0]) }) // v3 → drops v1 from selection
    const selected = view
      .getAllByTestId("prompt-version-row")
      .filter(r => r.dataset.versionSelected === "true")
      .map(r => r.dataset.versionId)
    expect(selected.sort()).toEqual(["2", "3"])
  })

  it("ESC key closes the drawer", () => {
    const fetcher = vi.fn().mockResolvedValue(makeListResponse([]))
    const onClose = vi.fn()
    render(<PromptVersionDrawer open={true} onClose={onClose} fetchVersions={fetcher} />)
    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }))
    })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it("backdrop click + close button both invoke onClose", () => {
    const fetcher = vi.fn().mockResolvedValue(makeListResponse([]))
    const onClose = vi.fn()
    const view = render(
      <PromptVersionDrawer open={true} onClose={onClose} fetchVersions={fetcher} />,
    )
    act(() => { fireEvent.click(view.getByTestId("prompt-version-drawer-backdrop")) })
    expect(onClose).toHaveBeenCalledTimes(1)
    act(() => { fireEvent.click(view.getByTestId("prompt-version-drawer-close")) })
    expect(onClose).toHaveBeenCalledTimes(2)
  })

  it("respects a custom subtypes prop", () => {
    const fetcher = vi.fn().mockResolvedValue(makeListResponse([]))
    const subs: PromptAgentSubtype[] = [
      { value: "alpha", label: "Alpha", group: "Custom" },
      { value: "beta", label: "Beta", group: "Custom" },
    ]
    const view = render(
      <PromptVersionDrawer
        open={true}
        onClose={() => {}}
        fetchVersions={fetcher}
        subtypes={subs}
        defaultAgentType="alpha"
      />,
    )
    const select = view.getByTestId("prompt-version-drawer-agent-select") as HTMLSelectElement
    expect(select.querySelectorAll("option")).toHaveLength(2)
    expect(select.value).toBe("alpha")
  })
})
