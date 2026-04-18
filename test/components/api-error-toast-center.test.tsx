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

// ── row 194: offline → 「網路連線中斷」info toast + retry indicator ───────
//
// `request()` classifies a TypeError out of fetch() as kind=offline (DNS
// failure, no network, browser offline). Idempotent GETs retry twice with
// 1s + 2s backoff before the terminal error is emitted on the bus, so
// driving the test means flushing ~10s of fake timers.
//
// The toast lives until the operator dismisses OR the browser fires an
// `online` event. On `online`, the toast triggers `window.location.reload()`
// so any failed initial fetches re-fire automatically.
describe("ApiErrorToastCenter — offline (row 194)", () => {
  const originalLocation = window.location

  beforeEach(() => {
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

  async function driveOffline() {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    global.fetch = vi
      .fn()
      .mockImplementation(() => Promise.reject(new TypeError("Failed to fetch"))) as unknown as typeof fetch
    const p = getHealth().catch((e) => e)
    // Idempotent GET: 1s + 2s backoff retries before terminal failure.
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
    const result = await p
    expect(result).toBeInstanceOf(ApiError)
    return result as ApiError
  }

  it("renders an info toast「網路連線中斷」when fetch rejects with TypeError", async () => {
    render(<ApiErrorToastCenter />)
    await driveOffline()

    const toast = await screen.findByTestId("api-error-toast-offline")
    expect(toast).toBeInTheDocument()
    expect(screen.getByText("網路連線中斷")).toBeInTheDocument()
    expect(screen.getByText("INFO")).toBeInTheDocument()
    expect(screen.getByText("OFFLINE")).toBeInTheDocument()
    expect(screen.getByText(/嘗試重新連線/)).toBeInTheDocument()
  })

  it("shows a spinning retry indicator", async () => {
    render(<ApiErrorToastCenter />)
    await driveOffline()

    const indicator = await screen.findByTestId("api-error-retry-offline")
    expect(indicator).toBeInTheDocument()
    expect(indicator).toHaveTextContent(/網路恢復/)
  })

  it("does NOT auto-dismiss after the warning/error windows", async () => {
    render(<ApiErrorToastCenter />)
    await driveOffline()

    expect(await screen.findByTestId("api-error-toast-offline")).toBeInTheDocument()
    // Past 5s warning window AND 10s error window — toast must persist
    // until network recovery or explicit dismissal.
    await act(async () => { await vi.advanceTimersByTimeAsync(20_000) })
    expect(screen.queryByTestId("api-error-toast-offline")).toBeInTheDocument()
  })

  it("coalesces repeated offline errors into a single toast", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    global.fetch = vi
      .fn()
      .mockImplementation(() => Promise.reject(new TypeError("Failed to fetch"))) as unknown as typeof fetch
    render(<ApiErrorToastCenter />)

    // Fire three failed requests concurrently — three terminal `offline`
    // errors hit the bus, but the component should coalesce them.
    const p1 = getHealth().catch(() => undefined)
    const p2 = getHealth().catch(() => undefined)
    const p3 = getHealth().catch(() => undefined)
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
    await Promise.all([p1, p2, p3])

    expect(screen.getAllByTestId("api-error-toast-offline")).toHaveLength(1)
  })

  it("reloads the page when the browser fires `online`", async () => {
    render(<ApiErrorToastCenter />)
    await driveOffline()
    await screen.findByTestId("api-error-toast-offline")

    const reloadSpy = window.location.reload as unknown as ReturnType<typeof vi.fn>
    expect(reloadSpy).not.toHaveBeenCalled()

    await act(async () => {
      window.dispatchEvent(new Event("online"))
    })
    expect(reloadSpy).toHaveBeenCalledTimes(1)
  })

  it("dismiss button removes the toast and detaches the online listener", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    render(<ApiErrorToastCenter />)
    await driveOffline()

    await screen.findByTestId("api-error-toast-offline")
    await user.click(screen.getByRole("button", { name: /dismiss/i }))
    expect(screen.queryByTestId("api-error-toast-offline")).toBeNull()

    // After dismiss, an `online` event must NOT trigger a reload.
    await act(async () => {
      window.dispatchEvent(new Event("online"))
    })
    const reloadSpy = window.location.reload as unknown as ReturnType<typeof vi.fn>
    expect(reloadSpy).not.toHaveBeenCalled()
  })
})
