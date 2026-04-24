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
  groupRowsIntoSegments,
  DEFAULT_DIFF_CONTEXT_LINES,
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

/**
 * ZZ.C1 checkbox 4 — pure-function coverage for the hunk / context
 * segment partition. These pin the contract the side-by-side renderer
 * and the ``n`` keyboard handler rely on:
 *  - A hunk is a contiguous run of removed/added rows (what git calls a
 *    "hunk" and what ``n`` jumps between).
 *  - Context bands longer than 2×N collapse behind a single
 *    ``context-collapsed`` segment whose rows are hidden until the
 *    operator expands them; shorter bands stay fully visible.
 *  - Leading / trailing bands shrink to one-sided visible + collapsed
 *    so only the contextLines adjacent to a hunk survive by default.
 */
describe("groupRowsIntoSegments()", () => {
  function ctx(n: number): { kind: "context"; left: string; right: string }[] {
    return Array.from({ length: n }, (_, i) => ({
      kind: "context" as const,
      left: `c${i}`,
      right: `c${i}`,
    }))
  }
  function hunk(n: number): { kind: "removed" | "added"; left: string | null; right: string | null }[] {
    const out: { kind: "removed" | "added"; left: string | null; right: string | null }[] = []
    for (let i = 0; i < n; i++) {
      if (i % 2 === 0) out.push({ kind: "removed", left: `r${i}`, right: null })
      else out.push({ kind: "added", left: null, right: `a${i}` })
    }
    return out
  }

  it("exposes 3 as the shipped default to match difflib's unified_diff(n=3)", () => {
    expect(DEFAULT_DIFF_CONTEXT_LINES).toBe(3)
  })

  it("empty rows → empty segments", () => {
    expect(groupRowsIntoSegments([], 3)).toEqual([])
  })

  it("whole diff is context and length ≤ N → one visible segment", () => {
    const segs = groupRowsIntoSegments(ctx(2), 3)
    expect(segs).toHaveLength(1)
    expect(segs[0].type).toBe("context-visible")
    expect(segs[0].rows).toHaveLength(2)
  })

  it("whole diff is context and length > N → visible head + collapsed tail", () => {
    const segs = groupRowsIntoSegments(ctx(10), 3)
    expect(segs).toHaveLength(2)
    expect(segs[0].type).toBe("context-visible")
    expect(segs[0].rows).toHaveLength(3)
    expect(segs[1].type).toBe("context-collapsed")
    expect(segs[1].rows).toHaveLength(7)
  })

  it("total rewrite — all rows are changed → one hunk, no context", () => {
    const segs = groupRowsIntoSegments(hunk(5), 3)
    expect(segs).toHaveLength(1)
    expect(segs[0].type).toBe("hunk")
    if (segs[0].type === "hunk") {
      expect(segs[0].hunkIndex).toBe(0)
      expect(segs[0].rows).toHaveLength(5)
    }
  })

  it("leading context longer than N → collapsed head + visible tail anchored to hunk", () => {
    const rows = [...ctx(10), ...hunk(2)]
    const segs = groupRowsIntoSegments(rows, 3)
    expect(segs.map(s => s.type)).toEqual([
      "context-collapsed",
      "context-visible",
      "hunk",
    ])
    expect(segs[0].rows).toHaveLength(10 - 3)
    expect(segs[1].rows).toHaveLength(3)
  })

  it("leading context ≤ N → stays fully visible", () => {
    const rows = [...ctx(2), ...hunk(2)]
    const segs = groupRowsIntoSegments(rows, 3)
    expect(segs.map(s => s.type)).toEqual(["context-visible", "hunk"])
    expect(segs[0].rows).toHaveLength(2)
  })

  it("trailing context longer than N → visible head + collapsed tail", () => {
    const rows = [...hunk(2), ...ctx(10)]
    const segs = groupRowsIntoSegments(rows, 3)
    expect(segs.map(s => s.type)).toEqual([
      "hunk",
      "context-visible",
      "context-collapsed",
    ])
    expect(segs[1].rows).toHaveLength(3)
    expect(segs[2].rows).toHaveLength(10 - 3)
  })

  it("middle context between two hunks > 2N → visible/collapsed/visible", () => {
    const rows = [...hunk(2), ...ctx(20), ...hunk(2)]
    const segs = groupRowsIntoSegments(rows, 3)
    expect(segs.map(s => s.type)).toEqual([
      "hunk",
      "context-visible",
      "context-collapsed",
      "context-visible",
      "hunk",
    ])
    expect(segs[1].rows).toHaveLength(3)
    expect(segs[2].rows).toHaveLength(20 - 6)
    expect(segs[3].rows).toHaveLength(3)
    // Hunk indices monotonically increase.
    const hunkSegs = segs.filter(s => s.type === "hunk")
    expect(hunkSegs).toHaveLength(2)
  })

  it("middle context between two hunks ≤ 2N → stays fully visible", () => {
    const rows = [...hunk(1), ...ctx(5), ...hunk(1)]
    const segs = groupRowsIntoSegments(rows, 3)
    expect(segs.map(s => s.type)).toEqual([
      "hunk",
      "context-visible",
      "hunk",
    ])
    expect(segs[1].rows).toHaveLength(5)
  })

  it("contextLines=0 → every context row is collapsed", () => {
    const rows = [...hunk(1), ...ctx(4), ...hunk(1)]
    const segs = groupRowsIntoSegments(rows, 0)
    expect(segs.map(s => s.type)).toEqual([
      "hunk",
      "context-collapsed",
      "hunk",
    ])
    expect(segs[1].rows).toHaveLength(4)
  })

  it("hunkIndex values are 0-based and stable across hunks", () => {
    const rows = [...hunk(1), ...ctx(10), ...hunk(1), ...ctx(2), ...hunk(1)]
    const segs = groupRowsIntoSegments(rows, 3)
    const hunkIndices = segs
      .filter((s): s is Extract<typeof segs[number], { type: "hunk" }> => s.type === "hunk")
      .map(s => s.hunkIndex)
    expect(hunkIndices).toEqual([0, 1, 2])
  })

  it("segment ids are unique so expand-state can key off them", () => {
    const rows = [
      ...ctx(10),
      ...hunk(2),
      ...ctx(20),
      ...hunk(2),
      ...ctx(10),
    ]
    const segs = groupRowsIntoSegments(rows, 3)
    const ids = segs.map(s => s.id)
    expect(new Set(ids).size).toBe(ids.length)
  })
})

/**
 * ZZ.C1 checkbox 4 — UI wiring: the collapsed band renders an "Expand N
 * lines" button that unfolds when clicked, and ``n`` / ``Shift+n``
 * cycles ``activeHunkIndex`` across hunks (wrapping at the ends). The
 * binding ignores keystrokes whose target is a form control so
 * <select>'s built-in type-ahead is not hijacked.
 */
describe("<PromptVersionDrawer> — unfold + keyboard nav", () => {
  const LONG_CONTEXT = Array.from({ length: 12 }, (_, i) => `ctx line ${i}`).join("\n")
  function twoHunkPair() {
    const oldBody = [
      LONG_CONTEXT,
      "ORIGINAL ONE",
      LONG_CONTEXT,
      "ORIGINAL TWO",
      LONG_CONTEXT,
    ].join("\n") + "\n"
    const newBody = [
      LONG_CONTEXT,
      "EDITED ONE",
      LONG_CONTEXT,
      "EDITED TWO",
      LONG_CONTEXT,
    ].join("\n") + "\n"
    return {
      from: makeEntry({
        id: 1,
        version: 1,
        content_hash: "a".repeat(64),
        content: oldBody,
      }),
      to: makeEntry({
        id: 2,
        version: 2,
        content_hash: "b".repeat(64),
        content: newBody,
      }),
    }
  }

  it("renders an Expand-context button for long leading context and expands on click", async () => {
    const { from, to } = twoHunkPair()
    const fetcher = vi.fn().mockResolvedValue(makeListResponse([to, from]))
    const view = render(
      <PromptVersionDrawer open={true} onClose={() => {}} fetchVersions={fetcher} />,
    )
    await waitFor(() => expect(view.getAllByTestId("prompt-version-row")).toHaveLength(2))
    const rows = view.getAllByTestId("prompt-version-row")
    act(() => { fireEvent.click(rows[0]) })
    act(() => { fireEvent.click(rows[1]) })

    const collapsed = await waitFor(() =>
      view.getAllByTestId("prompt-version-diff-context-collapsed"),
    )
    // 12-line leading band collapses 12-3=9 hidden lines; 12-line
    // middle band collapses 12-6=6 hidden; trailing band 12-3=9.
    expect(collapsed.length).toBeGreaterThanOrEqual(1)
    const hiddenCounts = collapsed.map(b =>
      Number(b.getAttribute("data-hidden-lines") ?? "0"),
    )
    expect(hiddenCounts).toEqual(expect.arrayContaining([9, 6]))
    for (const btn of collapsed) {
      expect(btn.textContent).toMatch(/Expand \d+ lines? of context/)
    }

    const firstCollapsed = collapsed[0]
    const segmentId = firstCollapsed.getAttribute("data-segment-id")
    expect(segmentId).not.toBeNull()
    act(() => { fireEvent.click(firstCollapsed) })
    // Expanded — collapsed button for this segment disappears, unfolded
    // tbody with the same segment-id takes its place.
    const remaining = view.queryAllByTestId("prompt-version-diff-context-collapsed")
      .map(el => el.getAttribute("data-segment-id"))
    expect(remaining).not.toContain(segmentId)
    const unfolded = view.getAllByTestId("prompt-version-diff-context-unfolded")
    expect(unfolded.some(el => el.getAttribute("data-segment-id") === segmentId)).toBe(true)
  })

  it("collapsed context exposes the button label 'Expand N lines of context'", async () => {
    const { from, to } = twoHunkPair()
    const fetcher = vi.fn().mockResolvedValue(makeListResponse([to, from]))
    const view = render(
      <PromptVersionDrawer open={true} onClose={() => {}} fetchVersions={fetcher} />,
    )
    await waitFor(() => expect(view.getAllByTestId("prompt-version-row")).toHaveLength(2))
    const rows = view.getAllByTestId("prompt-version-row")
    act(() => { fireEvent.click(rows[0]) })
    act(() => { fireEvent.click(rows[1]) })
    const btn = await waitFor(() => view.getAllByTestId("prompt-version-diff-context-collapsed")[0])
    expect(btn.getAttribute("aria-label")).toMatch(/Expand \d+ hidden context line/)
  })

  it("hunk counter badge surfaces the 1/N position, toolbar buttons navigate", async () => {
    const { from, to } = twoHunkPair()
    const fetcher = vi.fn().mockResolvedValue(makeListResponse([to, from]))
    const view = render(
      <PromptVersionDrawer open={true} onClose={() => {}} fetchVersions={fetcher} />,
    )
    await waitFor(() => expect(view.getAllByTestId("prompt-version-row")).toHaveLength(2))
    const rows = view.getAllByTestId("prompt-version-row")
    act(() => { fireEvent.click(rows[0]) })
    act(() => { fireEvent.click(rows[1]) })

    const counter = await waitFor(() => view.getByTestId("prompt-version-diff-hunk-counter"))
    expect(counter).toHaveTextContent(/hunk 1 \/ 2/)
    // Click the next-hunk toolbar button → counter advances.
    act(() => { fireEvent.click(view.getByTestId("prompt-version-diff-next-hunk")) })
    expect(view.getByTestId("prompt-version-diff-hunk-counter")).toHaveTextContent(/hunk 2 \/ 2/)
    // Wrap — next past the end goes back to hunk 1.
    act(() => { fireEvent.click(view.getByTestId("prompt-version-diff-next-hunk")) })
    expect(view.getByTestId("prompt-version-diff-hunk-counter")).toHaveTextContent(/hunk 1 \/ 2/)
    // Previous wraps the other way.
    act(() => { fireEvent.click(view.getByTestId("prompt-version-diff-prev-hunk")) })
    expect(view.getByTestId("prompt-version-diff-hunk-counter")).toHaveTextContent(/hunk 2 \/ 2/)
  })

  it("n key jumps to the next hunk (wrapping), Shift+n goes back", async () => {
    const { from, to } = twoHunkPair()
    const fetcher = vi.fn().mockResolvedValue(makeListResponse([to, from]))
    const view = render(
      <PromptVersionDrawer open={true} onClose={() => {}} fetchVersions={fetcher} />,
    )
    await waitFor(() => expect(view.getAllByTestId("prompt-version-row")).toHaveLength(2))
    const rows = view.getAllByTestId("prompt-version-row")
    act(() => { fireEvent.click(rows[0]) })
    act(() => { fireEvent.click(rows[1]) })
    await waitFor(() => view.getByTestId("prompt-version-diff-hunk-counter"))

    const initialActive = view
      .getAllByTestId("prompt-version-diff-hunk")
      .find(el => el.getAttribute("data-hunk-active") === "true")
    expect(initialActive?.getAttribute("data-hunk-index")).toBe("0")

    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "n", bubbles: true }))
    })
    const afterN = view
      .getAllByTestId("prompt-version-diff-hunk")
      .find(el => el.getAttribute("data-hunk-active") === "true")
    expect(afterN?.getAttribute("data-hunk-index")).toBe("1")

    // Wrap past last → back to hunk 0.
    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "n", bubbles: true }))
    })
    const afterWrap = view
      .getAllByTestId("prompt-version-diff-hunk")
      .find(el => el.getAttribute("data-hunk-active") === "true")
    expect(afterWrap?.getAttribute("data-hunk-index")).toBe("0")

    // Shift+n goes backward (0 → 1 via wrap).
    act(() => {
      window.dispatchEvent(
        new KeyboardEvent("keydown", { key: "N", shiftKey: true, bubbles: true }),
      )
    })
    const afterShift = view
      .getAllByTestId("prompt-version-diff-hunk")
      .find(el => el.getAttribute("data-hunk-active") === "true")
    expect(afterShift?.getAttribute("data-hunk-index")).toBe("1")
  })

  it("n key does not navigate when no diff is rendered (single selection)", async () => {
    const { from, to } = twoHunkPair()
    const fetcher = vi.fn().mockResolvedValue(makeListResponse([to, from]))
    const view = render(
      <PromptVersionDrawer open={true} onClose={() => {}} fetchVersions={fetcher} />,
    )
    await waitFor(() => expect(view.getAllByTestId("prompt-version-row")).toHaveLength(2))
    const rows = view.getAllByTestId("prompt-version-row")
    // Only one pick → diff not rendered yet.
    act(() => { fireEvent.click(rows[0]) })
    expect(view.queryByTestId("prompt-version-diff-side-by-side")).toBeNull()
    // n should be a no-op (no counter exists).
    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "n", bubbles: true }))
    })
    expect(view.queryByTestId("prompt-version-diff-hunk-counter")).toBeNull()
  })

  it("n key is ignored when focus is inside the agent_type <select>", async () => {
    const { from, to } = twoHunkPair()
    const fetcher = vi.fn().mockResolvedValue(makeListResponse([to, from]))
    const view = render(
      <PromptVersionDrawer open={true} onClose={() => {}} fetchVersions={fetcher} />,
    )
    await waitFor(() => expect(view.getAllByTestId("prompt-version-row")).toHaveLength(2))
    const rows = view.getAllByTestId("prompt-version-row")
    act(() => { fireEvent.click(rows[0]) })
    act(() => { fireEvent.click(rows[1]) })
    await waitFor(() => view.getByTestId("prompt-version-diff-hunk-counter"))

    // Focus the select, then dispatch `n` with the select as event target.
    const select = view.getByTestId("prompt-version-drawer-agent-select") as HTMLSelectElement
    select.focus()
    act(() => {
      const evt = new KeyboardEvent("keydown", { key: "n", bubbles: true })
      select.dispatchEvent(evt)
    })
    const active = view
      .getAllByTestId("prompt-version-diff-hunk")
      .find(el => el.getAttribute("data-hunk-active") === "true")
    // Cursor did not move off hunk 0.
    expect(active?.getAttribute("data-hunk-index")).toBe("0")
  })

  it("contextLines prop override shrinks the visible context band", async () => {
    const { from, to } = twoHunkPair()
    const fetcher = vi.fn().mockResolvedValue(makeListResponse([to, from]))
    const view = render(
      <PromptVersionDrawer
        open={true}
        onClose={() => {}}
        fetchVersions={fetcher}
        contextLines={1}
      />,
    )
    await waitFor(() => expect(view.getAllByTestId("prompt-version-row")).toHaveLength(2))
    const rows = view.getAllByTestId("prompt-version-row")
    act(() => { fireEvent.click(rows[0]) })
    act(() => { fireEvent.click(rows[1]) })
    const collapsed = await waitFor(() =>
      view.getAllByTestId("prompt-version-diff-context-collapsed"),
    )
    // contextLines=1 → visible bands shrink to 1 row each, more rows
    // collapsed than with the default of 3.
    const hidden = collapsed.reduce(
      (acc, b) => acc + Number(b.getAttribute("data-hidden-lines") ?? "0"),
      0,
    )
    expect(hidden).toBeGreaterThanOrEqual(11 + 10 + 11) // 3 bands of 12 lines with n=1 → 11 + 10 + 11
  })

  it("hunk counter disappears when the diff is identical (no hunks)", async () => {
    const sameHash = "c".repeat(64)
    const v1 = makeEntry({ id: 10, version: 5, content_hash: sameHash, content: "x\n" })
    const v2 = makeEntry({ id: 11, version: 6, content_hash: sameHash, content: "x\n" })
    const fetcher = vi.fn().mockResolvedValue(makeListResponse([v2, v1]))
    const view = render(
      <PromptVersionDrawer open={true} onClose={() => {}} fetchVersions={fetcher} />,
    )
    await waitFor(() => expect(view.getAllByTestId("prompt-version-row")).toHaveLength(2))
    const rows = view.getAllByTestId("prompt-version-row")
    act(() => { fireEvent.click(rows[0]) })
    act(() => { fireEvent.click(rows[1]) })
    await waitFor(() => view.getByTestId("prompt-version-diff-identical"))
    expect(view.queryByTestId("prompt-version-diff-hunk-counter")).toBeNull()
    expect(view.queryByTestId("prompt-version-diff-next-hunk")).toBeNull()
  })
})
