/**
 * BS.8.6 — CustomEntryForm contract tests.
 *
 * Locks the surface the platforms page wires up:
 *   1. Empty list renders the "No custom catalog entries yet" hint.
 *   2. Toolbar count reflects the rows.
 *   3. List is sorted newest-first by `created_at`.
 *   4. Per-row Edit button opens the inline form pre-populated.
 *   5. Per-row Remove → confirm overlay → Confirm calls onRemove.
 *   6. Per-row Remove → Cancel does not call onRemove.
 *   7. Add custom entry button opens the inline form (create mode).
 *   8. Form submit calls onCreate with the right payload (operator).
 *   9. Form rejects an obviously-bad URL before calling onCreate.
 *  10. Form rejects malformed sha256.
 *  11. Form rejects a duplicate id (in-memory uniqueness check).
 *  12. URL ping button surfaces ping result via testid.
 *  13. depends_on multi-select toggles add to the form payload.
 *  14. parseSizeBytesInput / formatSizeBytes / pickAvailableDependsOnIds
 *      pure helpers handle the obvious cases.
 *  15. validateCustomEntryFormFields enforces operator-row required-cols.
 */

import * as React from "react"
import { afterEach, describe, expect, it, vi } from "vitest"
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react"

import {
  CustomEntryForm,
  formatSizeBytes,
  parseSizeBytesInput,
  pickAvailableDependsOnIds,
  validateCustomEntryFormFields,
} from "@/components/omnisight/custom-entry-form"
import type { CatalogEntryDetail } from "@/lib/api"

const ENTRY_OLDER: CatalogEntryDetail = {
  id: "acme-sdk-old",
  source: "operator",
  schema_version: 1,
  tenant_id: "t-abc",
  vendor: "Acme",
  family: "embedded",
  display_name: "Acme SDK (old)",
  version: "0.9.0",
  install_method: "shell_script",
  install_url: "https://x.test/old.tar.gz",
  sha256: "0".repeat(64),
  size_bytes: 1024 * 1024,
  depends_on: [],
  metadata: { license: "MIT" },
  hidden: false,
  created_at: "2026-04-25T10:00:00Z",
  updated_at: "2026-04-25T10:00:00Z",
}

const ENTRY_NEWER: CatalogEntryDetail = {
  id: "acme-sdk-new",
  source: "override",
  schema_version: 1,
  tenant_id: "t-abc",
  vendor: "Acme",
  family: "embedded",
  display_name: "Acme SDK (new)",
  version: "1.0.0",
  install_method: "shell_script",
  install_url: "https://x.test/new.tar.gz",
  sha256: "1".repeat(64),
  size_bytes: 100 * 1024 * 1024,
  depends_on: ["acme-sdk-old"],
  metadata: { license: "Apache-2.0" },
  hidden: false,
  created_at: "2026-04-27T10:00:00Z",
  updated_at: "2026-04-27T10:00:00Z",
}

const SHIPPED: CatalogEntryDetail = {
  id: "shipped-base",
  source: "shipped",
  schema_version: 1,
  tenant_id: null,
  vendor: "Yocto Project",
  family: "embedded",
  display_name: "Shipped base",
  version: "5.0",
  install_method: "noop",
  install_url: null,
  sha256: null,
  size_bytes: null,
  depends_on: [],
  metadata: {},
  hidden: false,
  created_at: "2026-01-01T10:00:00Z",
  updated_at: "2026-01-01T10:00:00Z",
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe("BS.8.6 — CustomEntryForm", () => {
  it("renders the empty hint when no entries are supplied", () => {
    render(<CustomEntryForm entries={[]} allEntries={[]} />)
    expect(screen.getByTestId("custom-entry-form-empty")).toBeTruthy()
    expect(screen.getByTestId("custom-entry-form-count").textContent).toMatch(
      /0 custom entries/,
    )
  })

  it("renders one row per entry, newest first", () => {
    render(
      <CustomEntryForm
        entries={[ENTRY_OLDER, ENTRY_NEWER]}
        allEntries={[ENTRY_OLDER, ENTRY_NEWER]}
      />,
    )
    expect(screen.getByTestId("custom-entry-form-count").textContent).toMatch(
      /2 custom entries/,
    )
    const list = screen.getByTestId("custom-entry-form-list")
    const rows = list.querySelectorAll("li[data-entry-id]")
    expect(rows.length).toBe(2)
    expect((rows[0] as HTMLElement).dataset.entryId).toBe(ENTRY_NEWER.id)
    expect((rows[1] as HTMLElement).dataset.entryId).toBe(ENTRY_OLDER.id)
  })

  it("Edit button opens the inline form pre-populated with the row's values", () => {
    render(
      <CustomEntryForm
        entries={[ENTRY_NEWER]}
        allEntries={[ENTRY_NEWER]}
      />,
    )
    fireEvent.click(
      screen.getByTestId(`custom-entry-form-row-edit-${ENTRY_NEWER.id}`),
    )
    const form = screen.getByTestId("custom-entry-form-form")
    expect(form.getAttribute("data-form-mode")).toBe("edit")
    const idInput = screen.getByTestId(
      "custom-entry-form-field-id",
    ) as HTMLInputElement
    expect(idInput.value).toBe(ENTRY_NEWER.id)
    expect(idInput.disabled).toBe(true)
    const vendor = screen.getByTestId(
      "custom-entry-form-field-vendor",
    ) as HTMLInputElement
    expect(vendor.value).toBe(ENTRY_NEWER.vendor!)
    const license = screen.getByTestId(
      "custom-entry-form-field-license",
    ) as HTMLInputElement
    expect(license.value).toBe("Apache-2.0")
  })

  it("Remove → confirm overlay → Confirm calls onRemove", async () => {
    const onRemove = vi.fn().mockResolvedValue(undefined)
    render(
      <CustomEntryForm
        entries={[ENTRY_NEWER]}
        allEntries={[ENTRY_NEWER]}
        onRemove={onRemove}
      />,
    )
    fireEvent.click(
      screen.getByTestId(`custom-entry-form-row-remove-${ENTRY_NEWER.id}`),
    )
    expect(
      screen.getByTestId(`custom-entry-form-row-confirm-${ENTRY_NEWER.id}`),
    ).toBeTruthy()
    expect(onRemove).not.toHaveBeenCalled()
    fireEvent.click(
      screen.getByTestId(
        `custom-entry-form-row-confirm-delete-${ENTRY_NEWER.id}`,
      ),
    )
    await waitFor(() => expect(onRemove).toHaveBeenCalledTimes(1))
    expect(onRemove.mock.calls[0]![0]).toEqual(ENTRY_NEWER)
  })

  it("Cancel on the confirm overlay does not invoke onRemove", () => {
    const onRemove = vi.fn().mockResolvedValue(undefined)
    render(
      <CustomEntryForm
        entries={[ENTRY_NEWER]}
        allEntries={[ENTRY_NEWER]}
        onRemove={onRemove}
      />,
    )
    fireEvent.click(
      screen.getByTestId(`custom-entry-form-row-remove-${ENTRY_NEWER.id}`),
    )
    fireEvent.click(
      screen.getByTestId(
        `custom-entry-form-row-confirm-cancel-${ENTRY_NEWER.id}`,
      ),
    )
    expect(
      screen.queryByTestId(`custom-entry-form-row-confirm-${ENTRY_NEWER.id}`),
    ).toBeNull()
    expect(onRemove).not.toHaveBeenCalled()
  })

  it("Add custom entry button opens an inline form in create mode", () => {
    render(<CustomEntryForm entries={[]} allEntries={[]} />)
    fireEvent.click(screen.getByTestId("custom-entry-form-add-button"))
    const form = screen.getByTestId("custom-entry-form-form")
    expect(form.getAttribute("data-form-mode")).toBe("create")
    const idInput = screen.getByTestId(
      "custom-entry-form-field-id",
    ) as HTMLInputElement
    expect(idInput.disabled).toBe(false)
  })

  it("submitting the create form invokes onCreate with the built payload", async () => {
    const onCreate = vi.fn().mockResolvedValue(ENTRY_NEWER)
    render(
      <CustomEntryForm
        entries={[]}
        allEntries={[SHIPPED]}
        onCreate={onCreate}
      />,
    )
    fireEvent.click(screen.getByTestId("custom-entry-form-add-button"))
    fireEvent.change(screen.getByTestId("custom-entry-form-field-id"), {
      target: { value: "acme-new" },
    })
    fireEvent.change(screen.getByTestId("custom-entry-form-field-vendor"), {
      target: { value: "Acme" },
    })
    fireEvent.change(screen.getByTestId("custom-entry-form-field-family"), {
      target: { value: "embedded" },
    })
    fireEvent.change(
      screen.getByTestId("custom-entry-form-field-display-name"),
      { target: { value: "Acme New" } },
    )
    fireEvent.change(screen.getByTestId("custom-entry-form-field-version"), {
      target: { value: "1.0.0" },
    })
    fireEvent.change(
      screen.getByTestId("custom-entry-form-field-install-method"),
      { target: { value: "shell_script" } },
    )
    fireEvent.change(
      screen.getByTestId("custom-entry-form-field-install-url"),
      { target: { value: "https://x.test/sdk.tar.gz" } },
    )
    fireEvent.change(screen.getByTestId("custom-entry-form-field-sha256"), {
      target: { value: "a".repeat(64) },
    })
    fireEvent.change(screen.getByTestId("custom-entry-form-field-license"), {
      target: { value: "MIT" },
    })
    fireEvent.change(
      screen.getByTestId("custom-entry-form-field-size-bytes"),
      { target: { value: "100MB" } },
    )
    // Toggle a depends_on option
    fireEvent.click(
      screen.getByTestId(
        `custom-entry-form-depends-on-checkbox-${SHIPPED.id}`,
      ),
    )
    fireEvent.click(screen.getByTestId("custom-entry-form-form-submit"))
    await waitFor(() => expect(onCreate).toHaveBeenCalledTimes(1))
    const payload = onCreate.mock.calls[0]![0] as Record<string, unknown>
    expect(payload.id).toBe("acme-new")
    expect(payload.vendor).toBe("Acme")
    expect(payload.family).toBe("embedded")
    expect(payload.install_method).toBe("shell_script")
    expect(payload.install_url).toBe("https://x.test/sdk.tar.gz")
    expect(payload.sha256).toBe("a".repeat(64))
    expect(payload.size_bytes).toBe(100 * 1000 * 1000)
    expect(payload.depends_on).toEqual([SHIPPED.id])
    expect((payload.metadata as Record<string, unknown>).license).toBe("MIT")
  })

  it("rejects an obviously-bad URL before calling onCreate", async () => {
    const onCreate = vi.fn().mockResolvedValue(ENTRY_NEWER)
    render(
      <CustomEntryForm entries={[]} allEntries={[]} onCreate={onCreate} />,
    )
    fireEvent.click(screen.getByTestId("custom-entry-form-add-button"))
    fireEvent.change(screen.getByTestId("custom-entry-form-field-id"), {
      target: { value: "acme-new" },
    })
    fireEvent.change(screen.getByTestId("custom-entry-form-field-vendor"), {
      target: { value: "Acme" },
    })
    fireEvent.change(screen.getByTestId("custom-entry-form-field-family"), {
      target: { value: "embedded" },
    })
    fireEvent.change(
      screen.getByTestId("custom-entry-form-field-display-name"),
      { target: { value: "Acme New" } },
    )
    fireEvent.change(screen.getByTestId("custom-entry-form-field-version"), {
      target: { value: "1.0.0" },
    })
    fireEvent.change(
      screen.getByTestId("custom-entry-form-field-install-method"),
      { target: { value: "shell_script" } },
    )
    fireEvent.change(
      screen.getByTestId("custom-entry-form-field-install-url"),
      { target: { value: "ftp://x.test/sdk.tar.gz" } },
    )
    fireEvent.click(screen.getByTestId("custom-entry-form-form-submit"))
    await waitFor(() =>
      expect(screen.getByTestId("custom-entry-form-form-error")).toBeTruthy(),
    )
    expect(onCreate).not.toHaveBeenCalled()
  })

  it("rejects malformed sha256 before calling onCreate", async () => {
    const onCreate = vi.fn().mockResolvedValue(ENTRY_NEWER)
    render(
      <CustomEntryForm entries={[]} allEntries={[]} onCreate={onCreate} />,
    )
    fireEvent.click(screen.getByTestId("custom-entry-form-add-button"))
    fireEvent.change(screen.getByTestId("custom-entry-form-field-id"), {
      target: { value: "acme-new" },
    })
    fireEvent.change(screen.getByTestId("custom-entry-form-field-vendor"), {
      target: { value: "Acme" },
    })
    fireEvent.change(screen.getByTestId("custom-entry-form-field-family"), {
      target: { value: "embedded" },
    })
    fireEvent.change(
      screen.getByTestId("custom-entry-form-field-display-name"),
      { target: { value: "Acme New" } },
    )
    fireEvent.change(screen.getByTestId("custom-entry-form-field-version"), {
      target: { value: "1.0.0" },
    })
    fireEvent.change(
      screen.getByTestId("custom-entry-form-field-install-method"),
      { target: { value: "shell_script" } },
    )
    fireEvent.change(screen.getByTestId("custom-entry-form-field-sha256"), {
      target: { value: "tooshort" },
    })
    fireEvent.click(screen.getByTestId("custom-entry-form-form-submit"))
    await waitFor(() =>
      expect(
        screen.getByTestId("custom-entry-form-form-error").textContent,
      ).toMatch(/sha256/i),
    )
    expect(onCreate).not.toHaveBeenCalled()
  })

  it("rejects a duplicate id (in-memory uniqueness check) before calling onCreate", async () => {
    const onCreate = vi.fn().mockResolvedValue(ENTRY_NEWER)
    render(
      <CustomEntryForm
        entries={[ENTRY_OLDER]}
        allEntries={[ENTRY_OLDER]}
        onCreate={onCreate}
      />,
    )
    fireEvent.click(screen.getByTestId("custom-entry-form-add-button"))
    // Fill in a known existing id.
    fireEvent.change(screen.getByTestId("custom-entry-form-field-id"), {
      target: { value: ENTRY_OLDER.id },
    })
    fireEvent.change(screen.getByTestId("custom-entry-form-field-vendor"), {
      target: { value: "Acme" },
    })
    fireEvent.change(screen.getByTestId("custom-entry-form-field-family"), {
      target: { value: "embedded" },
    })
    fireEvent.change(
      screen.getByTestId("custom-entry-form-field-display-name"),
      { target: { value: "Dup" } },
    )
    fireEvent.change(screen.getByTestId("custom-entry-form-field-version"), {
      target: { value: "1.0.0" },
    })
    fireEvent.change(
      screen.getByTestId("custom-entry-form-field-install-method"),
      { target: { value: "shell_script" } },
    )
    fireEvent.click(screen.getByTestId("custom-entry-form-form-submit"))
    await waitFor(() =>
      expect(
        screen.getByTestId("custom-entry-form-form-error").textContent,
      ).toMatch(/already exists/i),
    )
    expect(onCreate).not.toHaveBeenCalled()
  })

  it("URL ping button reports ok / error / skipped via testid data-attr", async () => {
    const pingFn = vi
      .fn<(url: string) => Promise<{ kind: "ok"; status: number }>>()
      .mockResolvedValue({ kind: "ok", status: 200 })
    render(
      <CustomEntryForm entries={[]} allEntries={[]} pingUrlFn={pingFn} />,
    )
    fireEvent.click(screen.getByTestId("custom-entry-form-add-button"))
    fireEvent.change(
      screen.getByTestId("custom-entry-form-field-install-url"),
      { target: { value: "https://x.test/y" } },
    )
    fireEvent.click(screen.getByTestId("custom-entry-form-url-ping-button"))
    await waitFor(() => expect(pingFn).toHaveBeenCalledTimes(1))
    const result = await waitFor(() =>
      screen.getByTestId("custom-entry-form-url-ping-result"),
    )
    expect(result.getAttribute("data-ping-kind")).toBe("ok")
  })

  it("URL ping button surfaces an error result when the ping fn rejects", async () => {
    const pingFn = vi
      .fn<(url: string) => Promise<never>>()
      .mockRejectedValue(new Error("DNS fail"))
    render(
      <CustomEntryForm entries={[]} allEntries={[]} pingUrlFn={pingFn} />,
    )
    fireEvent.click(screen.getByTestId("custom-entry-form-add-button"))
    fireEvent.change(
      screen.getByTestId("custom-entry-form-field-install-url"),
      { target: { value: "https://broken.test/y" } },
    )
    fireEvent.click(screen.getByTestId("custom-entry-form-url-ping-button"))
    const result = await waitFor(() =>
      screen.getByTestId("custom-entry-form-url-ping-result"),
    )
    expect(result.getAttribute("data-ping-kind")).toBe("error")
    expect(result.textContent).toMatch(/DNS fail/)
  })

  it("renders the retry button when fetchError is supplied alongside onRetry", () => {
    const onRetry = vi.fn()
    render(
      <CustomEntryForm
        entries={[]}
        allEntries={[]}
        fetchError="boom"
        onRetry={onRetry}
      />,
    )
    expect(
      screen.getByTestId("custom-entry-form-fetch-error").textContent,
    ).toMatch(/boom/)
    fireEvent.click(screen.getByTestId("custom-entry-form-fetch-retry"))
    expect(onRetry).toHaveBeenCalledTimes(1)
  })

  it("parseSizeBytesInput accepts integer + suffix forms", () => {
    expect(parseSizeBytesInput("")).toBeNull()
    expect(parseSizeBytesInput("100")).toBe(100)
    expect(parseSizeBytesInput("1024")).toBe(1024)
    expect(parseSizeBytesInput("100MB")).toBe(100 * 1000 * 1000)
    expect(parseSizeBytesInput("100 MB")).toBe(100 * 1000 * 1000)
    expect(parseSizeBytesInput("1.5 GB")).toBe(1.5 * 1000 ** 3)
    expect(parseSizeBytesInput("1MiB")).toBe(1024 * 1024)
    expect(parseSizeBytesInput("100_000")).toBe(100000)
  })

  it("parseSizeBytesInput returns an error string on bad input", () => {
    expect(typeof parseSizeBytesInput("garbage")).toBe("string")
    expect(typeof parseSizeBytesInput("100 ZB")).toBe("string")
    expect(typeof parseSizeBytesInput("-100")).toBe("string")
  })

  it("formatSizeBytes picks the largest whole-unit fit", () => {
    expect(formatSizeBytes(null)).toBe("—")
    expect(formatSizeBytes(0)).toBe("0 B")
    expect(formatSizeBytes(500)).toBe("500 B")
    expect(formatSizeBytes(1000)).toBe("1.0 KB")
    expect(formatSizeBytes(1500)).toBe("1.5 KB")
    expect(formatSizeBytes(100 * 1000 * 1000)).toBe("100 MB")
  })

  it("pickAvailableDependsOnIds excludes self and hidden rows", () => {
    const hidden: CatalogEntryDetail = { ...SHIPPED, id: "hidden-row", hidden: true }
    const ids = pickAvailableDependsOnIds(
      [ENTRY_OLDER, ENTRY_NEWER, SHIPPED, hidden],
      ENTRY_NEWER.id,
    )
    expect(ids).toContain(ENTRY_OLDER.id)
    expect(ids).toContain(SHIPPED.id)
    expect(ids).not.toContain(ENTRY_NEWER.id)
    expect(ids).not.toContain("hidden-row")
  })

  it("validateCustomEntryFormFields enforces operator-row required cols", () => {
    const baseFields = {
      id: "valid-id",
      source: "operator" as const,
      vendor: "",
      family: "" as const,
      display_name: "",
      version: "",
      install_method: "" as const,
      install_url: "",
      sha256: "",
      size_bytes: "",
      license: "",
      depends_on: [],
    }
    expect(
      validateCustomEntryFormFields(baseFields, { mode: "create", existingIds: [] }),
    ).toMatch(/vendor/i)

    const goodOperator = {
      ...baseFields,
      vendor: "Acme",
      family: "embedded" as const,
      display_name: "Acme",
      version: "1.0.0",
      install_method: "shell_script" as const,
    }
    expect(
      validateCustomEntryFormFields(goodOperator, {
        mode: "create",
        existingIds: [],
      }),
    ).toBeNull()

    // Override mode does not require vendor / family / version.
    const overrideMin = {
      ...baseFields,
      source: "override" as const,
      vendor: "",
      family: "" as const,
      display_name: "",
      version: "",
      install_method: "" as const,
    }
    expect(
      validateCustomEntryFormFields(overrideMin, {
        mode: "create",
        existingIds: [],
      }),
    ).toBeNull()

    // Bad id always fails.
    expect(
      validateCustomEntryFormFields(
        { ...goodOperator, id: "" },
        { mode: "create", existingIds: [] },
      ),
    ).not.toBeNull()
  })

  it("renders the loading copy in the toolbar count when loading=true", () => {
    render(<CustomEntryForm entries={[]} allEntries={[]} loading />)
    expect(screen.getByTestId("custom-entry-form-count").textContent).toMatch(
      /Loading custom entries/,
    )
  })

  it("editing a row submits via onPatch with the modified payload", async () => {
    const onPatch = vi.fn().mockResolvedValue(ENTRY_NEWER)
    render(
      <CustomEntryForm
        entries={[ENTRY_NEWER]}
        allEntries={[ENTRY_NEWER]}
        onPatch={onPatch}
      />,
    )
    fireEvent.click(
      screen.getByTestId(`custom-entry-form-row-edit-${ENTRY_NEWER.id}`),
    )
    // Edit mode disables the source dropdown — operator can't relabel a row.
    const sourceSel = screen.getByTestId(
      "custom-entry-form-field-source",
    ) as HTMLSelectElement
    expect(sourceSel.disabled).toBe(true)
    // Tweak the version field and submit; onPatch should fire with the
    // modified payload, not onCreate.
    fireEvent.change(screen.getByTestId("custom-entry-form-field-version"), {
      target: { value: "1.5.0" },
    })
    fireEvent.click(screen.getByTestId("custom-entry-form-form-submit"))
    await waitFor(() => expect(onPatch).toHaveBeenCalledTimes(1))
    expect(onPatch.mock.calls[0]![0]).toBe(ENTRY_NEWER.id)
    const payload = onPatch.mock.calls[0]![1] as Record<string, unknown>
    expect(payload.version).toBe("1.5.0")
  })

  it("URL ping short-circuits on a bad URL scheme without calling pingUrlFn", async () => {
    const pingFn = vi.fn().mockResolvedValue({ kind: "ok", status: 200 })
    render(
      <CustomEntryForm entries={[]} allEntries={[]} pingUrlFn={pingFn} />,
    )
    fireEvent.click(screen.getByTestId("custom-entry-form-add-button"))
    // Non-empty URL → ping button enabled, but bad scheme → handleUrlPing
    // short-circuits with kind=error before calling pingFn.
    fireEvent.change(
      screen.getByTestId("custom-entry-form-field-install-url"),
      { target: { value: "ftp://invalid.example/y" } },
    )
    fireEvent.click(screen.getByTestId("custom-entry-form-url-ping-button"))
    const result = await waitFor(() =>
      screen.getByTestId("custom-entry-form-url-ping-result"),
    )
    expect(result.getAttribute("data-ping-kind")).toBe("error")
    expect(pingFn).not.toHaveBeenCalled()
  })

  it("size preset dropdown fills the size_bytes input with the picked value", () => {
    render(<CustomEntryForm entries={[]} allEntries={[]} />)
    fireEvent.click(screen.getByTestId("custom-entry-form-add-button"))
    const sizeInput = screen.getByTestId(
      "custom-entry-form-field-size-bytes",
    ) as HTMLInputElement
    expect(sizeInput.value).toBe("")
    const preset = screen.getByTestId(
      "custom-entry-form-field-size-preset",
    ) as HTMLSelectElement
    // SIZE_PRESETS uses binary units; "100 MB" preset = 100 * 1024 * 1024.
    const HUNDRED_MB = 100 * 1024 * 1024
    fireEvent.change(preset, { target: { value: String(HUNDRED_MB) } })
    expect(sizeInput.value).toBe(String(HUNDRED_MB))
  })

  it("toggling a depends_on checkbox flips the data-checked attr", () => {
    render(
      <CustomEntryForm
        entries={[]}
        allEntries={[SHIPPED, ENTRY_OLDER]}
      />,
    )
    fireEvent.click(screen.getByTestId("custom-entry-form-add-button"))
    const option = screen.getByTestId(
      `custom-entry-form-depends-on-option-${SHIPPED.id}`,
    )
    expect(option.getAttribute("data-checked")).toBe("false")
    const checkbox = screen.getByTestId(
      `custom-entry-form-depends-on-checkbox-${SHIPPED.id}`,
    )
    fireEvent.click(checkbox)
    expect(option.getAttribute("data-checked")).toBe("true")
    // Toggling again clears the selection.
    fireEvent.click(checkbox)
    expect(option.getAttribute("data-checked")).toBe("false")
  })

  it("override mode lets the operator submit with empty vendor/family/version", async () => {
    const onCreate = vi.fn().mockResolvedValue(ENTRY_NEWER)
    render(
      <CustomEntryForm entries={[]} allEntries={[]} onCreate={onCreate} />,
    )
    fireEvent.click(screen.getByTestId("custom-entry-form-add-button"))
    fireEvent.change(screen.getByTestId("custom-entry-form-field-id"), {
      target: { value: "shipped-base" },
    })
    fireEvent.change(screen.getByTestId("custom-entry-form-field-source"), {
      target: { value: "override" },
    })
    // Leave vendor / family / display_name / version / install_method blank.
    fireEvent.click(screen.getByTestId("custom-entry-form-form-submit"))
    await waitFor(() => expect(onCreate).toHaveBeenCalledTimes(1))
    const payload = onCreate.mock.calls[0]![0] as Record<string, unknown>
    expect(payload.id).toBe("shipped-base")
    expect(payload.source).toBe("override")
  })

  it("validateCustomEntryFormFields skips the unique-id check in edit mode", () => {
    const fields = {
      id: "acme-sdk-old",
      source: "operator" as const,
      vendor: "Acme",
      family: "embedded" as const,
      display_name: "Acme",
      version: "1.0.0",
      install_method: "shell_script" as const,
      install_url: "",
      sha256: "",
      size_bytes: "",
      license: "",
      depends_on: [],
    }
    // Even though existingIds contains the id, edit mode does not flag it.
    expect(
      validateCustomEntryFormFields(fields, {
        mode: "edit",
        existingIds: [fields.id],
      }),
    ).toBeNull()
    expect(
      validateCustomEntryFormFields(fields, {
        mode: "create",
        existingIds: [fields.id],
      }),
    ).toMatch(/already exists/i)
  })
})
