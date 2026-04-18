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
 *   - row 193: 502 bad_gateway / 503 service_unavailable
 *              → warning toast「服務暫時不可用」with a countdown and a
 *                full-page reload when the countdown expires (cancelable
 *                via dismiss).
 */

import { describe, expect, it, vi, afterEach, beforeEach } from "vitest"
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

// ── row 193: 502/503 → 「服務暫時不可用」toast + auto-retry ─────────────
//
// `request()` auto-retries 429/503 internally with backoff; 502 is also
// retried when the method is idempotent (GET is). By the time the toast
// fires, the fetch-level retries are exhausted — the toast exists to let
// the operator know that one last full-page reload is about to happen, and
// to give them a chance to cancel via dismiss.
describe("ApiErrorToastCenter — 502/503 auto-retry (row 193)", () => {
  const originalLocation = window.location

  beforeEach(() => {
    // JSDOM doesn't let us spy on the real `window.location.reload`, so we
    // replace the whole object with a stub whose `.reload` is a vi.fn.
    // Matches what test/lib/api-error-handler.test.ts does for `.assign`.
    Object.defineProperty(window, "location", {
      configurable: true,
      writable: true,
      value: {
        href: "http://localhost/dashboard",
        pathname: "/dashboard",
        search: "",
        origin: "http://localhost",
        assign: vi.fn(),
        reload: vi.fn(),
      },
    })
  })

  afterEach(() => {
    vi.useRealTimers()
    Object.defineProperty(window, "location", {
      configurable: true,
      writable: true,
      value: originalLocation,
    })
  })

  async function drive502({
    body = { detail: "upstream down" },
    headers,
  }: {
    body?: unknown
    headers?: Record<string, string>
  } = {}) {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    mockFetchAlways(502, body, headers)
    const p = getHealth().catch((e) => e)
    // Idempotent GET retries twice (1s + 2s backoff) for 5xx.
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
    const result = await p
    expect(result).toBeInstanceOf(ApiError)
    return result as ApiError
  }

  async function drive503({
    body = { detail: "maintenance" },
    headers,
  }: {
    body?: unknown
    headers?: Record<string, string>
  } = {}) {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    mockFetchAlways(503, body, headers)
    const p = getHealth().catch((e) => e)
    // 503 has its own retry branch (429/503 → Retry-After + exponential).
    // Default backoff: 1s + 2s = 3s across 2 retries → 5s is safe slack.
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
    const result = await p
    expect(result).toBeInstanceOf(ApiError)
    return result as ApiError
  }

  it("renders a warning toast「服務暫時不可用」on 502 response", async () => {
    render(<ApiErrorToastCenter />)
    await drive502()

    const toast = await screen.findByTestId("api-error-toast-bad_gateway")
    expect(toast).toBeInTheDocument()
    expect(screen.getByText("服務暫時不可用")).toBeInTheDocument()
    expect(screen.getByText("WARNING")).toBeInTheDocument()
    expect(screen.getByText("HTTP 502")).toBeInTheDocument()
    expect(screen.getByText(/後端服務無法回應/)).toBeInTheDocument()
  })

  it("renders a warning toast「服務暫時不可用」on 503 (non-bootstrap)", async () => {
    render(<ApiErrorToastCenter />)
    await drive503()

    const toast = await screen.findByTestId("api-error-toast-service_unavailable")
    expect(toast).toBeInTheDocument()
    expect(screen.getByText("服務暫時不可用")).toBeInTheDocument()
    expect(screen.getByText("HTTP 503")).toBeInTheDocument()
    expect(screen.getByText(/維護/)).toBeInTheDocument()
  })

  it("shows a visible auto-retry countdown that ticks down", async () => {
    render(<ApiErrorToastCenter />)
    await drive502()

    const countdown = await screen.findByTestId("api-error-countdown-bad_gateway")
    expect(countdown).toBeInTheDocument()
    // Initially shows a value close to 10s (exact value depends on timer
    // slack from the retry backoff, but must be positive and ≤ 10).
    const initialText = countdown.textContent || ""
    const initialMatch = initialText.match(/(\d+)\s*s/)
    expect(initialMatch).toBeTruthy()
    const initialSec = Number(initialMatch![1])
    expect(initialSec).toBeGreaterThan(0)
    expect(initialSec).toBeLessThanOrEqual(10)

    // Advance 3s worth of 1Hz ticks.
    await act(async () => { await vi.advanceTimersByTimeAsync(3000) })
    const laterText = screen.getByTestId("api-error-countdown-bad_gateway").textContent || ""
    const laterMatch = laterText.match(/(\d+)\s*s/)
    expect(laterMatch).toBeTruthy()
    expect(Number(laterMatch![1])).toBeLessThan(initialSec)
  })

  it("reloads the page when the countdown expires (auto-retry)", async () => {
    render(<ApiErrorToastCenter />)
    await drive502()

    const reloadSpy = window.location.reload as unknown as ReturnType<typeof vi.fn>
    expect(reloadSpy).not.toHaveBeenCalled()

    // Push past the 10s auto-retry window (plus slack for JSDOM scheduling).
    await act(async () => { await vi.advanceTimersByTimeAsync(11_000) })
    expect(reloadSpy).toHaveBeenCalledTimes(1)
  })

  it("dismiss button cancels the auto-retry (no page reload)", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    render(<ApiErrorToastCenter />)
    await drive502()

    await screen.findByTestId("api-error-toast-bad_gateway")
    await user.click(screen.getByRole("button", { name: /dismiss/i }))
    expect(screen.queryByTestId("api-error-toast-bad_gateway")).toBeNull()

    // Advance well past the auto-retry window — reload must NOT fire.
    await act(async () => { await vi.advanceTimersByTimeAsync(12_000) })
    const reloadSpy = window.location.reload as unknown as ReturnType<typeof vi.fn>
    expect(reloadSpy).not.toHaveBeenCalled()
  })

  it("does NOT render a 502/503 toast for 500 (scope isolation)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    mockFetchAlways(500, { detail: "boom", trace_id: "req_trace" })
    render(<ApiErrorToastCenter />)
    const p = getHealth().catch((e) => e)
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
    await p

    expect(screen.queryByTestId("api-error-toast-bad_gateway")).toBeNull()
    expect(screen.queryByTestId("api-error-toast-service_unavailable")).toBeNull()
  })
})
