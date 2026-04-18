import { describe, expect, it, vi, beforeEach, afterEach } from "vitest"
import { act, render, screen, fireEvent } from "@testing-library/react"

/**
 * Unit tests for the shared FUI error page component (B13 Part B, #339).
 *
 * NeuralGrid is stubbed out — its DOM has no behavior relevant to the
 * props-driven code paths we're exercising here and keeping it off the tree
 * avoids a flood of unrelated style children in the snapshot assertions.
 */
vi.mock("@/components/omnisight/neural-grid", () => ({
  NeuralGrid: () => <div data-testid="neural-grid-stub" />,
}))

import { ErrorPage, type ErrorCode } from "@/components/omnisight/error-page"

describe("ErrorPage", () => {
  const originalLocation = window.location

  beforeEach(() => {
    Object.defineProperty(window, "location", {
      configurable: true,
      value: {
        ...originalLocation,
        reload: vi.fn(),
        href: originalLocation.href,
      },
    })
  })

  afterEach(() => {
    Object.defineProperty(window, "location", {
      configurable: true,
      value: originalLocation,
    })
  })

  const codes: ErrorCode[] = [400, 401, 403, 404, 500, 502, 503]

  it.each(codes)(
    "renders the preset big display code for HTTP %s",
    (code) => {
      render(<ErrorPage code={code} />)
      expect(screen.getByTestId("error-page-display-code")).toHaveTextContent(
        String(code),
      )
    },
  )

  it("shows the preset title and friendly message when not overridden", () => {
    render(<ErrorPage code={404} />)
    expect(screen.getByText("找不到此頁面")).toBeInTheDocument()
    expect(
      screen.getByText(/此頁面不存在或已移除/),
    ).toBeInTheDocument()
  })

  it.each([
    [400, "請求格式有誤，請檢查輸入後重試。"],
    [401, "登入已過期，請重新登入。"],
    [403, "您沒有此頁面的存取權限，請聯繫管理員開通。"],
    [404, "此頁面不存在或已移除，請確認網址是否正確。"],
    [500, "系統發生內部錯誤，我們已收到通知。"],
    [502, "後端服務暫時不可用，請稍後重試。"],
    [503, "系統維護中，請稍後再試。"],
  ] as const)(
    "exposes the spec-defined friendly message for HTTP %s",
    (code, message) => {
      render(<ErrorPage code={code} />)
      expect(screen.getByText(message)).toBeInTheDocument()
    },
  )

  it("renders a copyable trace ID when supplied (500 preset)", () => {
    render(<ErrorPage code={500} traceId="req_abc-123" />)
    const badge = screen.getByTestId("error-page-trace-id")
    expect(badge).toBeInTheDocument()
    expect(badge).toHaveTextContent("req_abc-123")
  })

  it("omits the trace ID badge entirely when traceId is empty", () => {
    render(<ErrorPage code={500} />)
    expect(screen.queryByTestId("error-page-trace-id")).not.toBeInTheDocument()
  })

  it("renders an auto-retry countdown and reloads when it hits zero (502)", () => {
    vi.useFakeTimers()
    const reload = window.location.reload as unknown as ReturnType<typeof vi.fn>
    try {
      render(<ErrorPage code={502} autoRetrySeconds={2} />)
      const badge = screen.getByTestId("error-page-auto-retry")
      expect(badge).toHaveTextContent("2s")

      act(() => {
        vi.advanceTimersByTime(1000)
      })
      expect(badge).toHaveTextContent("1s")

      act(() => {
        vi.advanceTimersByTime(1000)
      })
      expect(badge).toHaveTextContent("0s")

      act(() => {
        vi.advanceTimersByTime(0)
      })
      expect(reload).toHaveBeenCalledTimes(1)
    } finally {
      vi.useRealTimers()
    }
  })

  it("swaps 503 to the bootstrap-required preset when bootstrapRequired is true", () => {
    render(<ErrorPage code={503} bootstrapRequired />)
    expect(screen.getByText("設定未完成")).toBeInTheDocument()
    expect(
      screen.getByText(/系統初始設定尚未完成/),
    ).toBeInTheDocument()
    const go = screen.getByRole("link", { name: /前往設定/ })
    expect(go).toHaveAttribute("href", "/setup-required")
    // Default maintenance copy must NOT leak through when bootstrap is active.
    expect(screen.queryByText("系統維護中，請稍後再試。")).not.toBeInTheDocument()
  })

  it("keeps 503 on the maintenance preset when bootstrapRequired is false", () => {
    render(<ErrorPage code={503} />)
    expect(screen.getByText("系統維護中")).toBeInTheDocument()
    expect(screen.queryByText("設定未完成")).not.toBeInTheDocument()
  })

  it("lets callers override title, friendlyMessage, and systemLabel", () => {
    render(
      <ErrorPage
        code={500}
        systemLabel="SYS.CUSTOM · LABEL"
        title="自訂標題"
        friendlyMessage={<span>自訂友善訊息</span>}
      />,
    )
    expect(screen.getByText("SYS.CUSTOM · LABEL")).toBeInTheDocument()
    expect(screen.getByText("自訂標題")).toBeInTheDocument()
    expect(screen.getByText("自訂友善訊息")).toBeInTheDocument()
  })

  it("keeps technical detail collapsed by default and reveals it on toggle", () => {
    render(
      <ErrorPage
        code={500}
        technicalDetail={
          <pre data-testid="trace">trace-id=abc-123</pre>
        }
      />,
    )

    // The disclosure button exists but the content is NOT in the DOM yet.
    expect(screen.queryByTestId("trace")).not.toBeInTheDocument()

    const toggle = screen.getByRole("button", { name: /技術詳情/ })
    expect(toggle).toHaveAttribute("aria-expanded", "false")

    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute("aria-expanded", "true")
    expect(screen.getByTestId("trace")).toBeInTheDocument()

    // Toggling back hides it again.
    fireEvent.click(toggle)
    expect(screen.queryByTestId("trace")).not.toBeInTheDocument()
  })

  it("renders defaultExpanded=true immediately", () => {
    render(
      <ErrorPage
        code={500}
        defaultExpanded
        technicalDetail={<div data-testid="detail-open">hello</div>}
      />,
    )
    expect(screen.getByTestId("detail-open")).toBeInTheDocument()
  })

  it("omits the disclosure button entirely when no technical detail is provided", () => {
    render(<ErrorPage code={404} />)
    expect(
      screen.queryByRole("button", { name: /技術詳情/ }),
    ).not.toBeInTheDocument()
  })

  it("renders href-based actions as anchor tags and preserves the label", () => {
    render(
      <ErrorPage
        code={401}
        actions={[
          { label: "登入", href: "/login" },
          { label: "回首頁", href: "/", variant: "secondary" },
        ]}
      />,
    )
    const login = screen.getByRole("link", { name: /登入/ })
    expect(login).toHaveAttribute("href", "/login")
    const home = screen.getByRole("link", { name: /回首頁/ })
    expect(home).toHaveAttribute("href", "/")
  })

  it("calls onClick handlers for action buttons", () => {
    const onRetry = vi.fn()
    render(
      <ErrorPage
        code={500}
        actions={[{ label: "重試", onClick: onRetry }]}
      />,
    )
    fireEvent.click(screen.getByRole("button", { name: /重試/ }))
    expect(onRetry).toHaveBeenCalledTimes(1)
  })

  it("adds external anchor attributes for external actions", () => {
    render(
      <ErrorPage
        code={403}
        actions={[
          {
            label: "聯繫管理員",
            href: "mailto:admin@example.com",
            external: true,
          },
        ]}
      />,
    )
    const link = screen.getByRole("link", { name: /聯繫管理員/ })
    expect(link).toHaveAttribute("target", "_blank")
    expect(link).toHaveAttribute("rel", expect.stringContaining("noreferrer"))
    expect(link).toHaveAttribute("href", "mailto:admin@example.com")
  })

  it("falls back to default actions when none are supplied", () => {
    // 401 default: login link + home link
    render(<ErrorPage code={401} />)
    expect(
      screen.getByRole("link", { name: /登入/ }),
    ).toHaveAttribute("href", "/login")
    expect(
      screen.getByRole("link", { name: /回首頁/ }),
    ).toHaveAttribute("href", "/")
  })

  it("default retry action for 5xx reloads the window", () => {
    const reload = window.location.reload as unknown as ReturnType<typeof vi.fn>
    render(<ErrorPage code={500} />)
    fireEvent.click(screen.getByRole("button", { name: /重試/ }))
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it("supports a custom displayCode (e.g. 'ERR' instead of numeric)", () => {
    render(<ErrorPage code={500} displayCode="ERR" />)
    expect(screen.getByTestId("error-page-display-code")).toHaveTextContent(
      "ERR",
    )
  })

  it("renders a custom footer when provided and suppresses the default", () => {
    render(
      <ErrorPage
        code={404}
        footer={<span data-testid="custom-footer">custom</span>}
      />,
    )
    expect(screen.getByTestId("custom-footer")).toBeInTheDocument()
    expect(
      screen.queryByText(/neural command center/i),
    ).not.toBeInTheDocument()
  })
})
