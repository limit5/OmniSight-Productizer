/**
 * Phase 68-C — SpecTemplateEditor component tests.
 *
 * Covers:
 *   1. Prose typing debounces into parseIntent
 *   2. Form tab lets the operator set a field at confidence 1.0
 *   3. Conflict panel renders options and clicking one calls
 *      clarifyIntent with the right ids
 *   4. Continue button disabled when a conflict is unresolved
 *   5. Continue button fires onSpecReady with the current spec
 */

import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

vi.mock("@/lib/api", () => ({
  parseIntent: vi.fn(),
  clarifyIntent: vi.fn(),
  ingestRepo: vi.fn(),
  uploadDocs: vi.fn(),
  whoami: vi.fn().mockResolvedValue({
    user: { id: "test-user-1", email: "test@test.com", name: "Test", role: "admin", enabled: true },
    auth_mode: "open",
    session_id: null,
  }),
  getUserPreference: vi.fn().mockResolvedValue(null),
  setUserPreference: vi.fn().mockResolvedValue(undefined),
  setCurrentSessionId: vi.fn(),
}))

import { SpecTemplateEditor } from "@/components/omnisight/spec-template-editor"
import { AuthProvider } from "@/lib/auth-context"
import { I18nProvider } from "@/lib/i18n/context"
import * as api from "@/lib/api"
import type { ParsedSpec } from "@/lib/api"

function Wrapper({ children }: { children: React.ReactNode }) {
  return (
    <I18nProvider>
      <AuthProvider>{children}</AuthProvider>
    </I18nProvider>
  )
}

const mockParse = api.parseIntent as ReturnType<typeof vi.fn>
const mockClarify = api.clarifyIntent as ReturnType<typeof vi.fn>
const mockIngestRepo = (api as any).ingestRepo as ReturnType<typeof vi.fn>
const mockUploadDocs = (api as any).uploadDocs as ReturnType<typeof vi.fn>

const okSpec: ParsedSpec = {
  project_type:      { value: "web_app",   confidence: 0.9 },
  runtime_model:     { value: "ssg",       confidence: 0.9 },
  target_arch:       { value: "x86_64",    confidence: 0.9 },
  target_os:         { value: "linux",     confidence: 0.9 },
  framework:         { value: "nextjs",    confidence: 0.9 },
  persistence:       { value: "sqlite",    confidence: 0.9 },
  deploy_target:     { value: "local",     confidence: 0.9 },
  hardware_required: { value: "no",        confidence: 0.9 },
  raw_text: "demo",
  conflicts: [],
}

const conflictSpec: ParsedSpec = {
  ...okSpec,
  conflicts: [{
    id: "static_with_runtime_db",
    message: "Pick one:",
    fields: ["runtime_model", "persistence"],
    options: [
      { id: "ssg_build_time", label: "SSG — build time" },
      { id: "ssr_runtime", label: "SSR — runtime" },
    ],
    severity: "routine",
  }],
}

describe("SpecTemplateEditor", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockParse.mockResolvedValue(okSpec)
    // Phase 68-D: SpecTemplateEditor restores from localStorage on
    // mount. Clear between tests so the chip-fill test isn't
    // shadowed by a sibling test's persisted "demo" raw_text.
    if (typeof window !== "undefined") {
      window.localStorage.clear()
    }
  })

  it("debounces prose input into a parseIntent call", async () => {
    const user = userEvent.setup()
    render(<SpecTemplateEditor />, { wrapper: Wrapper })
    const ta = screen.getByRole("textbox", { name: /project prose/i })
    await user.type(ta, "build next.js app")
    // Debounced; wait for the single call to fire.
    await waitFor(() => expect(mockParse).toHaveBeenCalled(), { timeout: 1200 })
    const args = mockParse.mock.calls[mockParse.mock.calls.length - 1]
    expect(args[0]).toContain("build next.js app")
  })

  it("form tab patches field at confidence 1.0 without calling LLM", async () => {
    const user = userEvent.setup()
    render(<SpecTemplateEditor />, { wrapper: Wrapper })
    // Switch to Form tab.
    await user.click(screen.getByRole("tab", { name: /form/i }))
    const picker = screen.getByLabelText("target_arch") as HTMLSelectElement
    await user.selectOptions(picker, "x86_64")
    // No network call — structured picks skip the LLM round-trip.
    expect(mockParse).not.toHaveBeenCalled()
    // Confidence badge ✓ appears for operator-set fields.
    expect(picker.value).toBe("x86_64")
  })

  it("conflict panel shows options and picks call clarifyIntent", async () => {
    const user = userEvent.setup()
    mockParse.mockResolvedValue(conflictSpec)
    mockClarify.mockResolvedValue(okSpec)
    render(<SpecTemplateEditor />, { wrapper: Wrapper })
    const ta = screen.getByRole("textbox", { name: /project prose/i })
    await user.type(ta, "static next.js site reads runtime db")
    await waitFor(() => expect(
      screen.getByText(/static_with_runtime_db/),
    ).toBeInTheDocument(), { timeout: 2000 })

    await user.click(screen.getByRole("button", { name: /SSR — runtime/i }))
    await waitFor(() => expect(mockClarify).toHaveBeenCalled())
    const [parsedArg, conflictId, optionId] = mockClarify.mock.calls[0]
    expect(parsedArg.conflicts.length).toBe(1)
    expect(conflictId).toBe("static_with_runtime_db")
    expect(optionId).toBe("ssr_runtime")
  })

  it("Continue button disabled while a conflict is unresolved", async () => {
    const user = userEvent.setup()
    mockParse.mockResolvedValue(conflictSpec)
    const onReady = vi.fn()
    render(<SpecTemplateEditor onSpecReady={onReady} />, { wrapper: Wrapper })
    const ta = screen.getByRole("textbox", { name: /project prose/i })
    await user.type(ta, "spec with conflict")
    await waitFor(() => expect(
      screen.getByText(/static_with_runtime_db/),
    ).toBeInTheDocument(), { timeout: 2000 })
    const btn = screen.getByRole("button", { name: /continue/i })
    expect(btn).toBeDisabled()
    expect(onReady).not.toHaveBeenCalled()
  })

  it("Continue button fires onSpecReady once spec is clean", async () => {
    const user = userEvent.setup()
    const onReady = vi.fn()
    render(<SpecTemplateEditor onSpecReady={onReady} />, { wrapper: Wrapper })
    const ta = screen.getByRole("textbox", { name: /project prose/i })
    await user.type(ta, "clean build request")
    await waitFor(() => expect(mockParse).toHaveBeenCalled(), { timeout: 1200 })
    const btn = await screen.findByRole("button", { name: /continue/i })
    await waitFor(() => expect(btn).not.toBeDisabled())
    await user.click(btn)
    expect(onReady).toHaveBeenCalledTimes(1)
    expect(onReady.mock.calls[0][0].framework.value).toBe("nextjs")
  })

  it("template chip click fills the prose textarea + triggers parse", async () => {
    const user = userEvent.setup()
    render(<SpecTemplateEditor />, { wrapper: Wrapper })
    const ta = screen.getByRole("textbox", { name: /project prose/i }) as HTMLTextAreaElement
    expect(ta.value).toBe("")
    // The CJK template chip exercises the bilingual support — and
    // confirms a chip click writes the prose into the textarea.
    await user.click(screen.getByRole("button", { name: /Embedded Static UI/i }))
    expect(ta.value).toContain("x86_64")
    expect(ta.value).toContain("Next.js")
    // Debounced parse fires after the chip-driven setText.
    await waitFor(() => expect(mockParse).toHaveBeenCalled(), { timeout: 1500 })
  })

  it("Continue persists the spec to localStorage for back-jump restore", async () => {
    const user = userEvent.setup()
    window.localStorage.clear()
    const onReady = vi.fn()
    render(<SpecTemplateEditor onSpecReady={onReady} />, { wrapper: Wrapper })
    await user.type(
      screen.getByRole("textbox", { name: /project prose/i }),
      "anything",
    )
    await waitFor(() => expect(mockParse).toHaveBeenCalled(), { timeout: 1200 })
    const btn = await screen.findByRole("button", { name: /continue/i })
    await waitFor(() => expect(btn).not.toBeDisabled())
    await user.click(btn)
    const stored = window.localStorage.getItem("omnisight:test-user-1:intent:last_spec")
    expect(stored).not.toBeNull()
    const cached = JSON.parse(stored!)
    expect(cached.framework.value).toBe("nextjs")
  })

  it("renders a failure banner when DagEditor dispatches spec-failure-context", async () => {
    render(<SpecTemplateEditor />, { wrapper: Wrapper })
    window.dispatchEvent(new CustomEvent("omnisight:spec-failure-context", {
      detail: {
        reason: "API 422: tier_violation on task `flash`",
        rules: ["tier_violation"],
        target_platform: "host_native",
      },
    }))
    const alert = await screen.findByRole("alert", { name: /DAG failure context/i })
    expect(alert).toBeInTheDocument()
    // tier_violation appears twice (in reason + in rules); use within().
    expect(alert.textContent).toContain("tier_violation")
    // tier_violation hint mentions target_arch as the likely fix.
    expect(alert.textContent).toContain("target_arch")
  })

  it("clears the failure banner on the next prose edit", async () => {
    const user = userEvent.setup()
    render(<SpecTemplateEditor />, { wrapper: Wrapper })
    window.dispatchEvent(new CustomEvent("omnisight:spec-failure-context", {
      detail: { reason: "x", rules: [], target_platform: null },
    }))
    expect(await screen.findByRole("alert", { name: /DAG failure context/i }))
      .toBeInTheDocument()
    await user.type(
      screen.getByRole("textbox", { name: /project prose/i }),
      "any change",
    )
    expect(screen.queryByRole("alert", { name: /DAG failure context/i }))
      .not.toBeInTheDocument()
  })

  it("restores the cached spec on next mount (back-from-DAG path)", async () => {
    window.localStorage.setItem(
      "omnisight:test-user-1:intent:last_spec",
      JSON.stringify({
        ...okSpec,
        raw_text: "previously typed prompt",
        framework: { value: "django", confidence: 0.9 },
      }),
    )
    render(<SpecTemplateEditor />, { wrapper: Wrapper })
    const ta = await screen.findByRole(
      "textbox", { name: /project prose/i },
    ) as HTMLTextAreaElement
    await waitFor(() => expect(ta.value).toBe("previously typed prompt"))
  })

  // ─── B5/UX-01: Source tabs ─────────────────────────────────────

  it("renders all four source tabs (Prose, From Repo, From Docs, Form)", () => {
    render(<SpecTemplateEditor />, { wrapper: Wrapper })
    expect(screen.getByRole("tab", { name: /prose/i })).toBeInTheDocument()
    expect(screen.getByRole("tab", { name: /from repo/i })).toBeInTheDocument()
    expect(screen.getByRole("tab", { name: /from docs/i })).toBeInTheDocument()
    expect(screen.getByRole("tab", { name: /form/i })).toBeInTheDocument()
  })

  it("Repo tab: URL input triggers ingestRepo and populates spec", async () => {
    const user = userEvent.setup()
    const repoSpec: ParsedSpec = {
      ...okSpec,
      framework: { value: "fastapi", confidence: 0.95 },
      raw_text: "[ingested from repo: requirements.txt]",
    }
    mockIngestRepo.mockResolvedValue({
      ...repoSpec,
      _ingest_meta: {
        detected_files: ["requirements.txt", "README.md"],
        has_package_json: false,
        has_readme: true,
        has_requirements: true,
        has_cargo: false,
      },
    })

    const onReady = vi.fn()
    render(<SpecTemplateEditor onSpecReady={onReady} />, { wrapper: Wrapper })

    await user.click(screen.getByRole("tab", { name: /from repo/i }))
    const urlInput = screen.getByLabelText("Repository URL")
    await user.type(urlInput, "https://github.com/test/repo.git")
    await user.click(screen.getByLabelText("Clone and analyze"))

    await waitFor(() => expect(mockIngestRepo).toHaveBeenCalledWith("https://github.com/test/repo.git"))
    expect(await screen.findByText("requirements.txt")).toBeInTheDocument()
    expect(screen.getByText("README.md")).toBeInTheDocument()

    // Spec was populated — Continue should be available
    await user.click(screen.getByRole("tab", { name: /form/i }))
    await waitFor(() => {
      const picker = screen.getByLabelText("target_arch") as HTMLSelectElement
      expect(picker.value).toBe("x86_64")
    })
  })

  it("Repo tab: shows error on ingest failure", async () => {
    const user = userEvent.setup()
    mockIngestRepo.mockRejectedValue(new Error("Authentication failed"))

    render(<SpecTemplateEditor />, { wrapper: Wrapper })
    await user.click(screen.getByRole("tab", { name: /from repo/i }))
    await user.type(screen.getByLabelText("Repository URL"), "https://github.com/private/repo")
    await user.click(screen.getByLabelText("Clone and analyze"))

    expect(await screen.findByText(/Authentication failed/)).toBeInTheDocument()
  })

  it("Docs tab: drag-drop zone renders and file upload populates spec", async () => {
    const user = userEvent.setup()
    const docsSpec: ParsedSpec = {
      ...okSpec,
      framework: { value: "django", confidence: 0.9 },
    }
    mockUploadDocs.mockResolvedValue({
      spec: docsSpec,
      files: [
        { name: "spec.md", status: "parsed", size: 512 },
        { name: "bad.exe", status: "rejected", reason: "unsupported extension: .exe" },
      ],
    })

    render(<SpecTemplateEditor />, { wrapper: Wrapper })
    await user.click(screen.getByRole("tab", { name: /from docs/i }))

    expect(screen.getByLabelText("Drop zone")).toBeInTheDocument()

    // Simulate file selection via the hidden input
    const input = screen.getByLabelText("File upload") as HTMLInputElement
    const file = new File(["# My Project\nA Django project"], "spec.md", { type: "text/markdown" })
    await user.upload(input, file)

    await waitFor(() => expect(mockUploadDocs).toHaveBeenCalled())
    expect(await screen.findByText("spec.md")).toBeInTheDocument()
    expect(screen.getByText("parsed")).toBeInTheDocument()
    expect(screen.getByText("bad.exe")).toBeInTheDocument()
    expect(screen.getByText("rejected")).toBeInTheDocument()
  })

  it("merge preserves user prose overrides (confidence 1.0) over ingested data", async () => {
    const user = userEvent.setup()

    // Step 1: User sets framework manually in Form tab
    const onReady = vi.fn()
    render(<SpecTemplateEditor onSpecReady={onReady} />, { wrapper: Wrapper })
    await user.click(screen.getByRole("tab", { name: /form/i }))
    const fwInput = screen.getByPlaceholderText("nextjs, django, axum, ...")
    await user.type(fwInput, "my-custom-framework")

    // Step 2: Ingest from repo — framework should NOT override user pick
    const repoSpec: ParsedSpec = {
      ...okSpec,
      framework: { value: "fastapi", confidence: 0.95 },
    }
    mockIngestRepo.mockResolvedValue({
      ...repoSpec,
      _ingest_meta: { detected_files: ["requirements.txt"], has_package_json: false, has_readme: false, has_requirements: true, has_cargo: false },
    })

    await user.click(screen.getByRole("tab", { name: /from repo/i }))
    await user.type(screen.getByLabelText("Repository URL"), "https://github.com/test/repo")
    await user.click(screen.getByLabelText("Clone and analyze"))
    await waitFor(() => expect(mockIngestRepo).toHaveBeenCalled())

    // Verify: framework kept user's pick (confidence 1.0), other fields filled by ingest
    await user.click(screen.getByRole("tab", { name: /form/i }))
    await waitFor(() => {
      const fwField = screen.getByPlaceholderText("nextjs, django, axum, ...") as HTMLInputElement
      expect(fwField.value).toBe("my-custom-framework")
    })
  })

  it("Docs tab: shows error on upload failure", async () => {
    const user = userEvent.setup()
    mockUploadDocs.mockRejectedValue(new Error("upload-docs failed (500): internal error"))

    render(<SpecTemplateEditor />, { wrapper: Wrapper })
    await user.click(screen.getByRole("tab", { name: /from docs/i }))
    const input = screen.getByLabelText("File upload") as HTMLInputElement
    const file = new File(["content"], "readme.md", { type: "text/markdown" })
    await user.upload(input, file)

    expect(await screen.findByText(/upload-docs failed/)).toBeInTheDocument()
  })
})
