/**
 * FX.2.3 — ApiKeyManagementPanel input a11y contract tests.
 *
 * Locks the label / aria-label contract added in FX.2.3 (audit row D31 in
 * docs/audit/2026-05-03-deep-audit.md):
 *
 *   1. The "Key name" input is reachable via its accessible name
 *      (sr-only ``<label>`` + ``aria-label``).
 *   2. The "Scopes" input is reachable via its accessible name.
 *   3. Each input has a unique ``id`` linked from a ``<label htmlFor>`` —
 *      so screen readers announce the field name on focus, not just the
 *      placeholder (which disappears once the user starts typing).
 *
 * WCAG 2.1 SC 3.3.2 (Labels or Instructions) and SC 4.1.2 (Name, Role,
 * Value) require programmatic labels for all form controls; placeholder
 * text alone is not a sufficient label.
 */

import * as React from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"

import { ApiKeyManagementPanel } from "@/components/omnisight/api-key-management-panel"

vi.mock("@/lib/api", () => ({
  listApiKeys: vi.fn().mockResolvedValue({ items: [], count: 0 }),
  createApiKey: vi.fn(),
  rotateApiKey: vi.fn(),
  revokeApiKey: vi.fn(),
  enableApiKey: vi.fn(),
  deleteApiKey: vi.fn(),
}))

beforeEach(() => {
  vi.clearAllMocks()
})

afterEach(() => {
  cleanup()
})

async function openCreateForm(): Promise<void> {
  // Wait for initial listApiKeys() to settle so the loading placeholder
  // is gone before we toggle the form open.
  await waitFor(() => {
    expect(screen.queryByText(/Loading\.\.\./i)).toBeNull()
  })
  fireEvent.click(screen.getByRole("button", { name: /New Key/i }))
}

describe("FX.2.3 — ApiKeyManagementPanel input a11y", () => {
  it("exposes the key name input via its accessible label", async () => {
    render(<ApiKeyManagementPanel />)
    await openCreateForm()

    // getByLabelText resolves through either ``<label htmlFor>`` or
    // ``aria-label`` — both must point screen readers at the same field.
    const input = screen.getByLabelText(/API key name/i)
    expect(input).toBeDefined()
    expect((input as HTMLInputElement).tagName).toBe("INPUT")
    expect((input as HTMLInputElement).type).toBe("text")
  })

  it("exposes the scopes input via its accessible label", async () => {
    render(<ApiKeyManagementPanel />)
    await openCreateForm()

    const input = screen.getByLabelText(/scopes/i)
    expect(input).toBeDefined()
    expect((input as HTMLInputElement).tagName).toBe("INPUT")
  })

  it("links each input to a <label htmlFor> with a stable unique id", async () => {
    render(<ApiKeyManagementPanel />)
    await openCreateForm()

    const nameInput = screen.getByLabelText(/API key name/i) as HTMLInputElement
    const scopesInput = screen.getByLabelText(/scopes/i) as HTMLInputElement

    // Both inputs must carry an id (otherwise htmlFor cannot resolve).
    expect(nameInput.id).toBeTruthy()
    expect(scopesInput.id).toBeTruthy()
    expect(nameInput.id).not.toEqual(scopesInput.id)

    // And there must be a <label htmlFor=...> pointing at each — the
    // sr-only label is the screen-reader-only sibling we added in FX.2.3.
    const nameLabel = document.querySelector(`label[for="${nameInput.id}"]`)
    const scopesLabel = document.querySelector(`label[for="${scopesInput.id}"]`)
    expect(nameLabel).not.toBeNull()
    expect(scopesLabel).not.toBeNull()
  })
})
