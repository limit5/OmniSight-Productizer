/**
 * B13 Part C (#339) — ApiErrorToastCenter tests.
 *
 * The component subscribes to the `onApiError` bus exported by `lib/api.ts`
 * and surfaces variant-aware toasts for each classified error kind. The
 * test drives the real bus — mocking `fetch` to return a shaped response
 * and calling a real `request()` path (`getHealth()`) — so classification
 * + emission are covered end-to-end.
 *
 * Rows covered here:
 *   - row 191: 403 forbidden     → warning toast「權限不足」
 *   - row 192: 500 server_error  → error toast「系統錯誤」with an
 *                                   expandable「技術詳情」region that
 *                                   reveals the trace ID.
 */

import { describe, expect, it, vi, afterEach } from "vitest"
import { act, render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { ApiErrorToastCenter } from "@/components/omnisight/api-error-toast-center"
import { ApiError, getHealth } from "@/lib/api"

function mockFetchOnce(status: number, body: unknown, headers: Record<string, string> = {}) {
  const text = typeof body === "string" ? body : JSON.stringify(body)
  const spy = vi.fn().mockResolvedValueOnce(
    new Response(text, {
      status,
      headers: { "Content-Type": "application/json", ...headers },
    }),
  )
  global.fetch = spy as unknown as typeof fetch
  return spy
}

function mockFetchAlways(status: number, body: unknown, headers: Record<string, string> = {}) {
  const text = typeof body === "string" ? body : JSON.stringify(body)
  const spy = vi.fn().mockImplementation(() =>
    Promise.resolve(
      new Response(text, {
        status,
        headers: { "Content-Type": "application/json", ...headers },
      }),
    ),
  )
  global.fetch = spy as unknown as typeof fetch
  return spy
}

describe("ApiErrorToastCenter — 403 forbidden (row 191)", () => {
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

describe("ApiErrorToastCenter — 500 server_error (row 192)", () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  async function drive500({
    body,
    headers,
  }: {
    body: unknown
    headers?: Record<string, string>
  }): Promise<ApiError> {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    mockFetchAlways(500, body, headers)
    const p = getHealth().catch((e) => e)
    // Idempotent GET retries up to twice with 1s + 2s backoff.
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
    const result = await p
    expect(result).toBeInstanceOf(ApiError)
    return result as ApiError
  }

  it("renders an error toast「系統錯誤」on 500 response", async () => {
    render(<ApiErrorToastCenter />)
    await drive500({ body: { detail: "boom", trace_id: "req_xyz_123" } })

    const toast = await screen.findByTestId("api-error-toast-server_error")
    expect(toast).toBeInTheDocument()
    expect(screen.getByText("系統錯誤")).toBeInTheDocument()
    expect(screen.getByText("ERROR")).toBeInTheDocument()
    expect(screen.getByText("HTTP 500")).toBeInTheDocument()
    expect(screen.getByText(/系統發生內部錯誤/)).toBeInTheDocument()
  })

  it("keeps the trace ID collapsed by default and reveals it on expand", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    render(<ApiErrorToastCenter />)
    await drive500({ body: { detail: "boom", trace_id: "req_xyz_123" } })

    // Collapsed by default — trace ID not in the DOM yet.
    expect(screen.queryByTestId("api-error-details-server_error")).toBeNull()
    expect(screen.queryByText("req_xyz_123")).toBeNull()

    const toggle = screen.getByTestId("api-error-toggle-server_error")
    expect(toggle).toHaveAttribute("aria-expanded", "false")
    expect(screen.getByText(/技術詳情/)).toBeInTheDocument()

    await user.click(toggle)

    expect(toggle).toHaveAttribute("aria-expanded", "true")
    const details = await screen.findByTestId("api-error-details-server_error")
    expect(details).toBeInTheDocument()
    const trace = screen.getByTestId("api-error-trace-server_error")
    expect(trace).toHaveTextContent("req_xyz_123")
    // Collapse again → details unmount.
    await user.click(toggle)
    expect(toggle).toHaveAttribute("aria-expanded", "false")
    expect(screen.queryByTestId("api-error-details-server_error")).toBeNull()
  })

  it("prefers the X-Trace-Id response header over parsed body trace_id", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    render(<ApiErrorToastCenter />)
    await drive500({
      body: { detail: "boom", trace_id: "from_body_xxx" },
      headers: { "X-Trace-Id": "from_header_yyy" },
    })

    await user.click(screen.getByTestId("api-error-toggle-server_error"))
    const trace = await screen.findByTestId("api-error-trace-server_error")
    expect(trace).toHaveTextContent("from_header_yyy")
    expect(trace).not.toHaveTextContent("from_body_xxx")
  })

  it("hides the expand toggle when the server did not provide a trace ID", async () => {
    render(<ApiErrorToastCenter />)
    await drive500({ body: { detail: "boom" } })

    await screen.findByTestId("api-error-toast-server_error")
    expect(screen.queryByTestId("api-error-toggle-server_error")).toBeNull()
    expect(screen.queryByText(/技術詳情/)).toBeNull()
  })

  it("auto-dismisses after 10 seconds (longer than warning toasts)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    render(<ApiErrorToastCenter />)
    mockFetchAlways(500, { detail: "boom", trace_id: "req_trace" })
    const p = getHealth().catch((e) => e)
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    await p

    expect(await screen.findByTestId("api-error-toast-server_error")).toBeInTheDocument()
    // 5s (warning dismiss) still not enough to clear an error toast.
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    expect(screen.queryByTestId("api-error-toast-server_error")).toBeInTheDocument()
    // Cross the 10s mark from toast birth.
    await act(async () => { await vi.advanceTimersByTimeAsync(6000) })
    expect(screen.queryByTestId("api-error-toast-server_error")).toBeNull()
  })

  it("does NOT render a forbidden toast for 500 (scope isolation)", async () => {
    render(<ApiErrorToastCenter />)
    await drive500({ body: { detail: "boom", trace_id: "req_trace" } })

    expect(screen.queryByTestId("api-error-toast-forbidden")).toBeNull()
  })
})
