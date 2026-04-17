/**
 * V0 #7 — Contract tests for `components/omnisight/workspace-chat.tsx`.
 *
 * Covers:
 *   - Type resolution: explicit prop vs. provider vs. missing both.
 *   - Header rendering (title + type chip + aria-label).
 *   - Empty-state vs. populated message log (role/text/attachments/annotations).
 *   - Composer text input, Enter-to-send, Shift+Enter newline.
 *   - Submit payload shape + workspaceType stamping.
 *   - Submit is disabled when composer is empty.
 *   - Submit is disabled while an async submission is in flight.
 *   - Successful submit clears text / attachments / annotation chips.
 *   - Failed submit keeps composer state intact for retry.
 *   - Attachment tray (file input path + drop path + remove).
 *   - Attachment size gate drops oversized files.
 *   - Annotation chips: toggle, multi-select, stale-id cleanup.
 *   - `disabled` prop fully gates input, attach, submit and chips.
 *   - Pure helpers: `filesToChatAttachments`, `defaultChatIdFactory`,
 *     `defaultNowIso`.
 */

import { describe, expect, it, vi } from "vitest"
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react"
import * as React from "react"

import {
  WorkspaceChat,
  WORKSPACE_CHAT_MAX_FILE_BYTES,
  defaultChatIdFactory,
  defaultNowIso,
  filesToChatAttachments,
  type WorkspaceChatAnnotation,
  type WorkspaceChatAttachment,
  type WorkspaceChatMessage,
  type WorkspaceChatSubmission,
} from "@/components/omnisight/workspace-chat"
import { WorkspaceProvider } from "@/components/omnisight/workspace-context"

// ─── Helpers ───────────────────────────────────────────────────────────────

function makeIdFactory(prefix = "id"): () => string {
  let counter = 0
  return () => `${prefix}-${++counter}`
}

function makeMessage(
  id: string,
  overrides: Partial<WorkspaceChatMessage> = {},
): WorkspaceChatMessage {
  return {
    id,
    role: "user",
    text: `Message ${id}`,
    createdAt: "2026-04-18T12:00:00.000Z",
    ...overrides,
  }
}

function silenceConsoleError<T>(fn: () => T): T {
  const spy = vi.spyOn(console, "error").mockImplementation(() => {})
  try {
    return fn()
  } finally {
    spy.mockRestore()
  }
}

// jsdom doesn't ship createObjectURL by default
beforeEach(() => {
  if (typeof URL.createObjectURL !== "function") {
    ;(URL as unknown as { createObjectURL: (b: Blob) => string }).createObjectURL =
      () => "blob:mock"
  }
  if (typeof URL.revokeObjectURL !== "function") {
    ;(URL as unknown as { revokeObjectURL: (u: string) => void }).revokeObjectURL = () => {}
  }
})

// ─── Helper-function tests ────────────────────────────────────────────────

describe("filesToChatAttachments", () => {
  it("maps files to attachment records with id / name / mime / size", () => {
    const file = new File(["hello"], "shot.png", { type: "image/png" })
    const idFactory = makeIdFactory("att")
    const [a] = filesToChatAttachments([file], idFactory)
    expect(a.id).toBe("att-1")
    expect(a.name).toBe("shot.png")
    expect(a.mimeType).toBe("image/png")
    expect(a.sizeBytes).toBe(5)
  })

  it("defaults missing mime types to application/octet-stream", () => {
    const file = new File(["x"], "thing.bin", { type: "" })
    const [a] = filesToChatAttachments([file], makeIdFactory())
    expect(a.mimeType).toBe("application/octet-stream")
  })

  it("sets a preview URL only for image/* files", () => {
    const img = new File(["x"], "a.png", { type: "image/png" })
    const text = new File(["x"], "b.txt", { type: "text/plain" })
    const [imgAtt, textAtt] = filesToChatAttachments([img, text], makeIdFactory())
    expect(typeof imgAtt.previewUrl).toBe("string")
    expect(textAtt.previewUrl).toBeNull()
  })

  it("drops files exceeding WORKSPACE_CHAT_MAX_FILE_BYTES", () => {
    const oversized = new File([new Uint8Array(WORKSPACE_CHAT_MAX_FILE_BYTES + 1)], "big.bin", {
      type: "application/octet-stream",
    })
    const ok = new File(["x"], "ok.txt", { type: "text/plain" })
    const result = filesToChatAttachments([oversized, ok], makeIdFactory())
    expect(result).toHaveLength(1)
    expect(result[0].name).toBe("ok.txt")
  })
})

describe("defaultChatIdFactory / defaultNowIso", () => {
  it("produces unique ids across consecutive calls", () => {
    const a = defaultChatIdFactory()
    const b = defaultChatIdFactory()
    expect(a).not.toEqual(b)
    expect(a.length).toBeGreaterThan(0)
  })

  it("falls back when crypto.randomUUID is missing", () => {
    const original = globalThis.crypto?.randomUUID
    try {
      Object.defineProperty(globalThis.crypto, "randomUUID", {
        value: undefined,
        configurable: true,
      })
      const id = defaultChatIdFactory()
      expect(id).toMatch(/^chat-/)
    } finally {
      if (original) {
        Object.defineProperty(globalThis.crypto, "randomUUID", {
          value: original,
          configurable: true,
        })
      }
    }
  })

  it("defaultNowIso returns an ISO-8601 string", () => {
    const iso = defaultNowIso()
    expect(() => new Date(iso).toISOString()).not.toThrow()
    expect(iso).toMatch(/^\d{4}-\d{2}-\d{2}T/)
  })
})

// ─── Type resolution ──────────────────────────────────────────────────────

describe("type resolution", () => {
  it("uses the explicit workspaceType prop when passed", () => {
    render(<WorkspaceChat workspaceType="mobile" />)
    const node = screen.getByTestId("workspace-chat")
    expect(node.getAttribute("data-workspace-type")).toBe("mobile")
  })

  it("uses the provider's type when no prop is passed", () => {
    render(
      <WorkspaceProvider type="software">
        <WorkspaceChat />
      </WorkspaceProvider>,
    )
    expect(screen.getByTestId("workspace-chat").getAttribute("data-workspace-type")).toBe(
      "software",
    )
  })

  it("prefers the explicit prop over the provider (caller wins)", () => {
    render(
      <WorkspaceProvider type="software">
        <WorkspaceChat workspaceType="web" />
      </WorkspaceProvider>,
    )
    expect(screen.getByTestId("workspace-chat").getAttribute("data-workspace-type")).toBe("web")
  })

  it("throws when neither a prop nor a provider is present", () => {
    silenceConsoleError(() => {
      expect(() => render(<WorkspaceChat />)).toThrowError(/could not resolve/)
    })
  })

  it("throws on an invalid workspaceType prop", () => {
    silenceConsoleError(() => {
      expect(() =>
        render(
          // @ts-expect-error — deliberate invalid string to exercise guard
          <WorkspaceChat workspaceType="bogus" />,
        ),
      ).toThrowError(/could not resolve/)
    })
  })
})

// ─── Header ───────────────────────────────────────────────────────────────

describe("header", () => {
  it("renders default title and workspace-type chip", () => {
    render(<WorkspaceChat workspaceType="web" />)
    expect(screen.getByText("Workspace chat")).toBeInTheDocument()
    expect(screen.getByTestId("workspace-chat-type").textContent).toBe("web")
  })

  it("allows a custom title via the title prop", () => {
    render(<WorkspaceChat workspaceType="web" title="Design Brief" />)
    expect(screen.getByText("Design Brief")).toBeInTheDocument()
    expect(screen.getByTestId("workspace-chat").getAttribute("aria-label")).toContain(
      "Design Brief",
    )
  })

  it("stamps aria-label with title + workspace type", () => {
    render(<WorkspaceChat workspaceType="mobile" />)
    const aria = screen.getByTestId("workspace-chat").getAttribute("aria-label") ?? ""
    expect(aria).toContain("Workspace chat")
    expect(aria).toContain("mobile")
  })
})

// ─── Message log ──────────────────────────────────────────────────────────

describe("message log", () => {
  it("shows an empty-state row when messages is empty", () => {
    render(<WorkspaceChat workspaceType="web" messages={[]} />)
    expect(screen.getByTestId("workspace-chat-empty")).toBeInTheDocument()
  })

  it("shows the empty-state when messages is undefined", () => {
    render(<WorkspaceChat workspaceType="web" />)
    expect(screen.getByTestId("workspace-chat-empty")).toBeInTheDocument()
  })

  it("renders messages in the given order with role / text / timestamp", () => {
    const messages: WorkspaceChatMessage[] = [
      makeMessage("m1", { role: "user", text: "Change the header" }),
      makeMessage("m2", { role: "agent", text: "Patch queued" }),
    ]
    render(<WorkspaceChat workspaceType="web" messages={messages} />)

    expect(screen.getByTestId("workspace-chat-message-m1")).toBeInTheDocument()
    expect(screen.getByTestId("workspace-chat-message-m2")).toBeInTheDocument()
    expect(screen.getByTestId("workspace-chat-message-role-m1").textContent).toBe("You")
    expect(screen.getByTestId("workspace-chat-message-role-m2").textContent).toBe("Agent")
    expect(screen.getByTestId("workspace-chat-message-text-m1").textContent).toBe(
      "Change the header",
    )
    expect(screen.getByTestId("workspace-chat-message-text-m2").textContent).toBe("Patch queued")
  })

  it("tags messages with role + pending via data attributes", () => {
    render(
      <WorkspaceChat
        workspaceType="web"
        messages={[
          makeMessage("p", { role: "user", pending: true }),
          makeMessage("s", { role: "system", text: "Agent disconnected" }),
        ]}
      />,
    )
    const pending = screen.getByTestId("workspace-chat-message-p")
    expect(pending.getAttribute("data-role")).toBe("user")
    expect(pending.getAttribute("data-pending")).toBe("true")
    const system = screen.getByTestId("workspace-chat-message-s")
    expect(system.getAttribute("data-role")).toBe("system")
    expect(system.getAttribute("data-pending")).toBe("false")
  })

  it("renders per-message attachments and annotation chips", () => {
    const attachments: WorkspaceChatAttachment[] = [
      { id: "a1", name: "shot.png", mimeType: "image/png", sizeBytes: 123, previewUrl: null },
    ]
    render(
      <WorkspaceChat
        workspaceType="web"
        messages={[
          makeMessage("m1", {
            attachments,
            annotationIds: ["hdr-1", "hdr-2"],
            text: "Look here",
          }),
        ]}
      />,
    )
    expect(screen.getByTestId("workspace-chat-message-attachment-m1-a1").textContent).toBe(
      "shot.png",
    )
    expect(screen.getByTestId("workspace-chat-message-annotation-m1-hdr-1").textContent).toBe(
      "@hdr-1",
    )
    expect(screen.getByTestId("workspace-chat-message-annotation-m1-hdr-2").textContent).toBe(
      "@hdr-2",
    )
  })

  it("omits attachment / annotation trays when the message has none", () => {
    render(
      <WorkspaceChat
        workspaceType="web"
        messages={[makeMessage("solo", { text: "text only" })]}
      />,
    )
    expect(screen.queryByTestId("workspace-chat-message-attachments-solo")).toBeNull()
    expect(screen.queryByTestId("workspace-chat-message-annotations-solo")).toBeNull()
  })
})

// ─── Composer: text + submit ──────────────────────────────────────────────

describe("composer text + submit", () => {
  it("submit button is disabled when the composer is empty", () => {
    render(<WorkspaceChat workspaceType="web" />)
    expect(
      (screen.getByTestId("workspace-chat-submit-button") as HTMLButtonElement).disabled,
    ).toBe(true)
  })

  it("enables submit once text is entered", () => {
    render(<WorkspaceChat workspaceType="web" />)
    fireEvent.change(screen.getByTestId("workspace-chat-input"), {
      target: { value: "Do the thing" },
    })
    expect(
      (screen.getByTestId("workspace-chat-submit-button") as HTMLButtonElement).disabled,
    ).toBe(false)
  })

  it("calls onSubmitTask with the composed submission payload", async () => {
    const onSubmit = vi.fn()
    render(<WorkspaceChat workspaceType="mobile" onSubmitTask={onSubmit} />)

    fireEvent.change(screen.getByTestId("workspace-chat-input"), {
      target: { value: "  build a dashboard  " },
    })
    fireEvent.click(screen.getByTestId("workspace-chat-submit-button"))

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
    const payload = onSubmit.mock.calls[0][0] as WorkspaceChatSubmission
    expect(payload.text).toBe("build a dashboard")
    expect(payload.attachments).toEqual([])
    expect(payload.annotationIds).toEqual([])
    expect(payload.workspaceType).toBe("mobile")
  })

  it("sends on Enter and preserves newline on Shift+Enter", async () => {
    const onSubmit = vi.fn()
    render(<WorkspaceChat workspaceType="web" onSubmitTask={onSubmit} />)
    const input = screen.getByTestId("workspace-chat-input") as HTMLTextAreaElement

    fireEvent.change(input, { target: { value: "line 1" } })
    fireEvent.keyDown(input, { key: "Enter", shiftKey: true })
    expect(onSubmit).not.toHaveBeenCalled()

    fireEvent.change(input, { target: { value: "line 1\nline 2" } })
    fireEvent.keyDown(input, { key: "Enter" })
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
    expect((onSubmit.mock.calls[0][0] as WorkspaceChatSubmission).text).toBe("line 1\nline 2")
  })

  it("clears the composer after a successful submission", async () => {
    const onSubmit = vi.fn(() => Promise.resolve())
    render(<WorkspaceChat workspaceType="web" onSubmitTask={onSubmit} />)
    const input = screen.getByTestId("workspace-chat-input") as HTMLTextAreaElement

    fireEvent.change(input, { target: { value: "hello" } })
    fireEvent.click(screen.getByTestId("workspace-chat-submit-button"))

    await waitFor(() => expect(input.value).toBe(""))
  })

  it("keeps composer state intact when onSubmitTask rejects", async () => {
    const onSubmit = vi.fn(() => Promise.reject(new Error("boom")))
    render(<WorkspaceChat workspaceType="web" onSubmitTask={onSubmit} />)
    const input = screen.getByTestId("workspace-chat-input") as HTMLTextAreaElement

    fireEvent.change(input, { target: { value: "retry me" } })
    fireEvent.click(screen.getByTestId("workspace-chat-submit-button"))

    await waitFor(() => expect(onSubmit).toHaveBeenCalled())
    // Give the failing promise a microtask to resolve into the catch path.
    await Promise.resolve()
    expect(input.value).toBe("retry me")
  })

  it("disables submit while the async submission is in flight", async () => {
    let resolveFn: (() => void) | undefined
    const onSubmit = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          resolveFn = resolve
        }),
    )
    render(<WorkspaceChat workspaceType="web" onSubmitTask={onSubmit} />)
    fireEvent.change(screen.getByTestId("workspace-chat-input"), {
      target: { value: "slow op" },
    })
    fireEvent.click(screen.getByTestId("workspace-chat-submit-button"))

    await waitFor(() =>
      expect(screen.getByTestId("workspace-chat").getAttribute("data-submitting")).toBe("true"),
    )
    expect(
      (screen.getByTestId("workspace-chat-submit-button") as HTMLButtonElement).disabled,
    ).toBe(true)

    await act(async () => {
      resolveFn?.()
    })
    await waitFor(() =>
      expect(screen.getByTestId("workspace-chat").getAttribute("data-submitting")).toBe("false"),
    )
  })
})

// ─── Attachments ──────────────────────────────────────────────────────────

describe("attachments", () => {
  it("adds files selected from the hidden file input", () => {
    render(
      <WorkspaceChat
        workspaceType="web"
        idFactory={makeIdFactory("att")}
      />,
    )
    const input = screen.getByTestId("workspace-chat-file-input") as HTMLInputElement
    const file = new File(["x"], "a.png", { type: "image/png" })
    fireEvent.change(input, { target: { files: [file] } })
    expect(screen.getByTestId("workspace-chat-attachment-att-1").textContent).toContain("a.png")
  })

  it("accepts files dropped onto the composer", () => {
    render(
      <WorkspaceChat
        workspaceType="web"
        idFactory={makeIdFactory("att")}
      />,
    )
    const composer = screen.getByTestId("workspace-chat-composer")
    const file = new File(["x"], "dropped.png", { type: "image/png" })
    fireEvent.dragOver(composer)
    expect(composer.getAttribute("data-dragging")).toBe("true")
    fireEvent.drop(composer, { dataTransfer: { files: [file] } })
    expect(composer.getAttribute("data-dragging")).toBe("false")
    expect(screen.getByTestId("workspace-chat-attachment-att-1").textContent).toContain(
      "dropped.png",
    )
  })

  it("removes an attachment when its X button is clicked", () => {
    render(
      <WorkspaceChat
        workspaceType="web"
        idFactory={makeIdFactory("att")}
      />,
    )
    const input = screen.getByTestId("workspace-chat-file-input") as HTMLInputElement
    fireEvent.change(input, {
      target: { files: [new File(["x"], "z.png", { type: "image/png" })] },
    })
    fireEvent.click(screen.getByTestId("workspace-chat-attachment-remove-att-1"))
    expect(screen.queryByTestId("workspace-chat-attachment-att-1")).toBeNull()
  })

  it("includes attachments in the submit payload and clears after success", async () => {
    const onSubmit = vi.fn(() => Promise.resolve())
    render(
      <WorkspaceChat
        workspaceType="web"
        idFactory={makeIdFactory("att")}
        onSubmitTask={onSubmit}
      />,
    )
    const input = screen.getByTestId("workspace-chat-file-input") as HTMLInputElement
    fireEvent.change(input, {
      target: { files: [new File(["x"], "fig.png", { type: "image/png" })] },
    })
    fireEvent.change(screen.getByTestId("workspace-chat-input"), {
      target: { value: "apply this mockup" },
    })
    fireEvent.click(screen.getByTestId("workspace-chat-submit-button"))

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
    const payload = onSubmit.mock.calls[0][0] as WorkspaceChatSubmission
    expect(payload.attachments).toHaveLength(1)
    expect(payload.attachments[0].name).toBe("fig.png")

    await waitFor(() => expect(screen.queryByTestId("workspace-chat-attachment-att-1")).toBeNull())
  })

  it("allows submit when ONLY attachments are present (no text)", async () => {
    const onSubmit = vi.fn()
    render(
      <WorkspaceChat
        workspaceType="web"
        idFactory={makeIdFactory("att")}
        onSubmitTask={onSubmit}
      />,
    )
    const input = screen.getByTestId("workspace-chat-file-input") as HTMLInputElement
    fireEvent.change(input, {
      target: { files: [new File(["x"], "fig.png", { type: "image/png" })] },
    })
    expect(
      (screen.getByTestId("workspace-chat-submit-button") as HTMLButtonElement).disabled,
    ).toBe(false)
    fireEvent.click(screen.getByTestId("workspace-chat-submit-button"))
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
    expect((onSubmit.mock.calls[0][0] as WorkspaceChatSubmission).text).toBe("")
  })

  it("uses a custom readAttachmentsFromFiles when supplied", () => {
    const reader = vi.fn(
      (files: File[]): WorkspaceChatAttachment[] =>
        files.map((f, i) => ({
          id: `custom-${i}`,
          name: `custom-${f.name}`,
          mimeType: f.type,
          sizeBytes: f.size,
          previewUrl: null,
        })),
    )
    render(
      <WorkspaceChat workspaceType="web" readAttachmentsFromFiles={reader} />,
    )
    const input = screen.getByTestId("workspace-chat-file-input") as HTMLInputElement
    fireEvent.change(input, {
      target: { files: [new File(["x"], "a.png", { type: "image/png" })] },
    })
    expect(reader).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId("workspace-chat-attachment-custom-0").textContent).toContain(
      "custom-a.png",
    )
  })
})

// ─── Annotation chips ─────────────────────────────────────────────────────

describe("annotation chips", () => {
  const anns: WorkspaceChatAnnotation[] = [
    { id: "ann-1", label: "Header", description: "Top nav region" },
    { id: "ann-2", label: "Hero" },
  ]

  it("does not render the tray when no annotations are provided", () => {
    render(<WorkspaceChat workspaceType="web" />)
    expect(screen.queryByTestId("workspace-chat-annotation-tray")).toBeNull()
  })

  it("renders one chip per annotation with label prefix", () => {
    render(<WorkspaceChat workspaceType="web" annotations={anns} />)
    expect(screen.getByTestId("workspace-chat-annotation-ann-1").textContent).toContain("@Header")
    expect(screen.getByTestId("workspace-chat-annotation-ann-2").textContent).toContain("@Hero")
  })

  it("toggles a chip's selected state on click", () => {
    render(<WorkspaceChat workspaceType="web" annotations={anns} />)
    const chip = screen.getByTestId("workspace-chat-annotation-ann-1")
    expect(chip.getAttribute("data-active")).toBe("false")
    fireEvent.click(chip)
    expect(chip.getAttribute("data-active")).toBe("true")
    fireEvent.click(chip)
    expect(chip.getAttribute("data-active")).toBe("false")
  })

  it("includes selected annotation ids in the submission payload", async () => {
    const onSubmit = vi.fn()
    render(
      <WorkspaceChat workspaceType="web" annotations={anns} onSubmitTask={onSubmit} />,
    )
    fireEvent.click(screen.getByTestId("workspace-chat-annotation-ann-1"))
    fireEvent.click(screen.getByTestId("workspace-chat-annotation-ann-2"))
    fireEvent.change(screen.getByTestId("workspace-chat-input"), {
      target: { value: "fix these" },
    })
    fireEvent.click(screen.getByTestId("workspace-chat-submit-button"))

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
    const payload = onSubmit.mock.calls[0][0] as WorkspaceChatSubmission
    expect(payload.annotationIds).toEqual(["ann-1", "ann-2"])
  })

  it("drops stale selections when the available annotation set shrinks", () => {
    const { rerender } = render(
      <WorkspaceChat workspaceType="web" annotations={anns} />,
    )
    fireEvent.click(screen.getByTestId("workspace-chat-annotation-ann-1"))
    fireEvent.click(screen.getByTestId("workspace-chat-annotation-ann-2"))

    rerender(
      <WorkspaceChat
        workspaceType="web"
        annotations={[anns[1]]}
      />,
    )
    // ann-1 vanished from the tray and its selection should have been dropped.
    expect(screen.queryByTestId("workspace-chat-annotation-ann-1")).toBeNull()
    expect(
      screen.getByTestId("workspace-chat-annotation-ann-2").getAttribute("data-active"),
    ).toBe("true")
  })

  it("enables submit when ONLY an annotation is selected (no text, no files)", () => {
    render(<WorkspaceChat workspaceType="web" annotations={anns} />)
    fireEvent.click(screen.getByTestId("workspace-chat-annotation-ann-1"))
    expect(
      (screen.getByTestId("workspace-chat-submit-button") as HTMLButtonElement).disabled,
    ).toBe(false)
  })
})

// ─── `disabled` prop ──────────────────────────────────────────────────────

describe("disabled prop", () => {
  it("gates input / attach / submit buttons", () => {
    render(
      <WorkspaceChat
        workspaceType="web"
        annotations={[{ id: "a", label: "A" }]}
        disabled
      />,
    )
    expect((screen.getByTestId("workspace-chat-input") as HTMLTextAreaElement).disabled).toBe(true)
    expect(
      (screen.getByTestId("workspace-chat-attach-button") as HTMLButtonElement).disabled,
    ).toBe(true)
    expect(
      (screen.getByTestId("workspace-chat-submit-button") as HTMLButtonElement).disabled,
    ).toBe(true)
    expect(
      (screen.getByTestId("workspace-chat-annotation-a") as HTMLButtonElement).disabled,
    ).toBe(true)
  })

  it("does not invoke onSubmitTask when disabled even with text present", () => {
    const onSubmit = vi.fn()
    // Disabled at render time — composer shouldn't accept input, and
    // programmatic Enter must not fire onSubmit.
    render(<WorkspaceChat workspaceType="web" disabled onSubmitTask={onSubmit} />)
    const input = screen.getByTestId("workspace-chat-input") as HTMLTextAreaElement
    fireEvent.keyDown(input, { key: "Enter" })
    expect(onSubmit).not.toHaveBeenCalled()
  })
})

// ─── Placeholder ──────────────────────────────────────────────────────────

describe("placeholder", () => {
  it("uses a per-type default when placeholder prop is omitted", () => {
    const { rerender } = render(<WorkspaceChat workspaceType="web" />)
    const webPlaceholder = (screen.getByTestId("workspace-chat-input") as HTMLTextAreaElement)
      .placeholder
    rerender(<WorkspaceChat workspaceType="software" />)
    const softwarePlaceholder = (screen.getByTestId(
      "workspace-chat-input",
    ) as HTMLTextAreaElement).placeholder
    expect(webPlaceholder).not.toEqual(softwarePlaceholder)
    expect(webPlaceholder.length).toBeGreaterThan(0)
    expect(softwarePlaceholder.length).toBeGreaterThan(0)
  })

  it("honours an explicit placeholder override", () => {
    render(<WorkspaceChat workspaceType="web" placeholder="hello there" />)
    expect(
      (screen.getByTestId("workspace-chat-input") as HTMLTextAreaElement).placeholder,
    ).toBe("hello there")
  })
})
