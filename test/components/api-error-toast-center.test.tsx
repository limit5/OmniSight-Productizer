/**
 * B13 Part C (#339, row 191) — ApiErrorToastCenter tests.
 *
 * The component subscribes to the `onApiError` bus exported by `lib/api.ts`
 * and surfaces a warning toast「權限不足」when a 403 response fires through
 * the global error handler. The test drives the real bus — mocking `fetch`
 * to return 403 and calling a real `request()` path (`getHealth()`) — so
 * classification + emission are covered end-to-end.
 */

import { describe, expect, it, vi, afterEach } from "vitest"
import { act, render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { ApiErrorToastCenter } from "@/components/omnisight/api-error-toast-center"
import { ApiError, getHealth } from "@/lib/api"

function mockFetchOnce(status: number, body: unknown) {
  const text = typeof body === "string" ? body : JSON.stringify(body)
  const spy = vi.fn().mockResolvedValueOnce(
    new Response(text, {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  )
  global.fetch = spy as unknown as typeof fetch
  return spy
}

describe("ApiErrorToastCenter — 403 forbidden", () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it("renders a warning toast「權限不足」on 403 response", async () => {
    render(<ApiErrorToastCenter />)
    mockFetchOnce(403, { detail: "nope" })

    await act(async () => {
      await expect(getHealth()).rejects.toBeInstanceOf(ApiError)
    })

    const toast = await screen.findByTestId("api-error-toast-forbidden")
    expect(toast).toBeInTheDocument()
    expect(screen.getByText("權限不足")).toBeInTheDocument()
    expect(screen.getByText("WARNING")).toBeInTheDocument()
    expect(screen.getByText("HTTP 403")).toBeInTheDocument()
    expect(screen.getByText(/存取權限/)).toBeInTheDocument()
  })

  it("does NOT render a toast for 500 server_error (outside 403 scope in row 191)", async () => {
    vi.useFakeTimers()
    render(<ApiErrorToastCenter />)
    // 500 retries twice with 1s + 2s backoff before terminally failing.
    global.fetch = vi.fn().mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify({ detail: "boom" }), {
          status: 500,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    ) as unknown as typeof fetch

    const p = getHealth().catch((e) => e)
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
    const result = await p
    expect(result).toBeInstanceOf(ApiError)

    expect(screen.queryByTestId("api-error-toast-forbidden")).toBeNull()
  })

  it("auto-dismisses after 5 seconds", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    render(<ApiErrorToastCenter />)
    mockFetchOnce(403, { detail: "nope" })
    await act(async () => {
      await expect(getHealth()).rejects.toBeInstanceOf(ApiError)
    })

    expect(await screen.findByTestId("api-error-toast-forbidden")).toBeInTheDocument()
    await act(async () => { await vi.advanceTimersByTimeAsync(5100) })
    expect(screen.queryByTestId("api-error-toast-forbidden")).toBeNull()
  })

  it("dismiss button removes the toast", async () => {
    const user = userEvent.setup()
    render(<ApiErrorToastCenter />)
    mockFetchOnce(403, { detail: "nope" })
    await act(async () => {
      await expect(getHealth()).rejects.toBeInstanceOf(ApiError)
    })

    await screen.findByTestId("api-error-toast-forbidden")
    await user.click(screen.getByRole("button", { name: /dismiss/i }))
    expect(screen.queryByTestId("api-error-toast-forbidden")).toBeNull()
  })
})
