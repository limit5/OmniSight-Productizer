/**
 * AS.7.1 / OP-1726 — `<AuthTurnstileWidget>` component tests.
 *
 * Pins:
 *   - Explicit siteKey override → used directly, no runtime fetch
 *   - Missing siteKey override → fetches the runtime config (OP-1726)
 *     and skips the widget when no key is served / mounts when one is
 *   - Provided siteKey → injects the Turnstile script tag
 *   - Cleanup removes the widget on unmount when turnstile is loaded
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { render, screen, cleanup, waitFor } from "@testing-library/react"

import { AuthTurnstileWidget } from "@/components/omnisight/auth/auth-turnstile-widget"

// OP-1726 — mock the runtime config fetch so the no-override path is
// deterministic (the override path below passes an explicit siteKey and
// never reaches the fetch). A full mock keeps the 261KB lib/api module
// out of the jsdom run.
const mockFetchBotChallengeConfig = vi.fn()
vi.mock("@/lib/api", () => ({
  DEFAULT_BOT_CHALLENGE_CONFIG: {
    provider: null,
    siteKey: null,
    enabled: false,
  },
  fetchBotChallengeConfig: () => mockFetchBotChallengeConfig(),
}))

beforeEach(() => {
  // Default: no key served (staging / unconfigured posture).
  mockFetchBotChallengeConfig.mockResolvedValue({
    provider: null,
    siteKey: null,
    enabled: false,
  })
})

afterEach(() => {
  cleanup()
  // Remove any test-injected scripts so subsequent tests start clean.
  document
    .querySelectorAll('script[data-as7-turnstile-loaded]')
    .forEach((s) => s.remove())
  delete (window as { turnstile?: unknown }).turnstile
  delete (window as { __as7TurnstileReady?: () => void }).__as7TurnstileReady
  mockFetchBotChallengeConfig.mockReset()
})

describe("AS.7.1 AuthTurnstileWidget", () => {
  it("renders the disabled-state surface when siteKey is null", () => {
    render(<AuthTurnstileWidget siteKey={null} onToken={() => undefined} />)
    const widget = screen.getByTestId("as7-turnstile-widget")
    expect(widget).toHaveAttribute("data-as7-turnstile", "disabled")
  })

  it("renders the disabled-state surface before the runtime config resolves", () => {
    // No override → the widget seeds from the DEFAULT (disabled) config
    // and renders disabled until the runtime fetch lands.
    render(<AuthTurnstileWidget onToken={() => undefined} />)
    const widget = screen.getByTestId("as7-turnstile-widget")
    expect(widget).toHaveAttribute("data-as7-turnstile", "disabled")
  })

  it("renders the loading container with siteKey supplied", () => {
    render(
      <AuthTurnstileWidget siteKey="test-key" onToken={() => undefined} />,
    )
    const widget = screen.getByTestId("as7-turnstile-widget")
    // Initially the widget is loading until the script callback fires.
    expect(["loading", "ready"]).toContain(
      widget.getAttribute("data-as7-turnstile"),
    )
  })

  it("injects the Turnstile script tag once when siteKey is provided", () => {
    render(
      <AuthTurnstileWidget siteKey="test-key" onToken={() => undefined} />,
    )
    const scripts = document.querySelectorAll(
      'script[data-as7-turnstile-loaded]',
    )
    expect(scripts.length).toBe(1)
    const src = scripts[0].getAttribute("src") || ""
    expect(src).toContain("challenges.cloudflare.com/turnstile/v0/api.js")
  })

  it("does NOT inject a second script when a second widget mounts", () => {
    render(
      <>
        <AuthTurnstileWidget siteKey="k1" onToken={() => undefined} />
        <AuthTurnstileWidget siteKey="k1" onToken={() => undefined} />
      </>,
    )
    const scripts = document.querySelectorAll(
      'script[data-as7-turnstile-loaded]',
    )
    expect(scripts.length).toBe(1)
  })

  // ── OP-1726: runtime-driven config (no siteKey override) ──

  it("skips the widget when the runtime config serves no key", async () => {
    mockFetchBotChallengeConfig.mockResolvedValue({
      provider: null,
      siteKey: null,
      enabled: false,
    })
    render(<AuthTurnstileWidget onToken={() => undefined} />)

    // Disabled before AND after the fetch resolves; no script injected.
    await waitFor(() =>
      expect(mockFetchBotChallengeConfig).toHaveBeenCalled(),
    )
    const widget = screen.getByTestId("as7-turnstile-widget")
    expect(widget).toHaveAttribute("data-as7-turnstile", "disabled")
    expect(
      document.querySelectorAll("script[data-as7-turnstile-loaded]").length,
    ).toBe(0)
  })

  it("mounts the widget when the runtime config serves a key", async () => {
    mockFetchBotChallengeConfig.mockResolvedValue({
      provider: "turnstile",
      siteKey: "runtime-key",
      enabled: true,
    })
    render(<AuthTurnstileWidget onToken={() => undefined} />)

    await waitFor(() => {
      const widget = screen.getByTestId("as7-turnstile-widget")
      expect(["loading", "ready"]).toContain(
        widget.getAttribute("data-as7-turnstile"),
      )
    })
    await waitFor(() =>
      expect(
        document.querySelectorAll("script[data-as7-turnstile-loaded]").length,
      ).toBe(1),
    )
  })

  it("reports the resolved config to onConfigResolved", async () => {
    mockFetchBotChallengeConfig.mockResolvedValue({
      provider: "turnstile",
      siteKey: "runtime-key",
      enabled: true,
    })
    const onConfigResolved = vi.fn()
    render(
      <AuthTurnstileWidget
        onToken={() => undefined}
        onConfigResolved={onConfigResolved}
      />,
    )
    await waitFor(() =>
      expect(onConfigResolved).toHaveBeenCalledWith(
        expect.objectContaining({ enabled: true, siteKey: "runtime-key" }),
      ),
    )
  })

  it("does NOT fetch the runtime config when a siteKey override is passed", () => {
    render(
      <AuthTurnstileWidget siteKey="explicit-key" onToken={() => undefined} />,
    )
    expect(mockFetchBotChallengeConfig).not.toHaveBeenCalled()
  })

  it("calls turnstile.render() once script becomes ready", async () => {
    const renderSpy = vi.fn(() => "widget-id-123")
    const removeSpy = vi.fn()
    // Pre-stub the Turnstile global before mount so the post-script-load
    // useEffect path is exercised.
    ;(window as { turnstile?: unknown }).turnstile = {
      render: renderSpy,
      remove: removeSpy,
      reset: vi.fn(),
    }

    const onToken = vi.fn()
    render(
      <AuthTurnstileWidget
        siteKey="test-key"
        onToken={onToken}
        action="test-action"
        theme="dark"
      />,
    )

    // The render-effect runs synchronously after mount when the
    // pre-stubbed global is present.
    await Promise.resolve()
    expect(renderSpy).toHaveBeenCalledTimes(1)
    const opts = renderSpy.mock.calls[0][1] as Record<string, unknown>
    expect(opts.sitekey).toBe("test-key")
    expect(opts.action).toBe("test-action")
    expect(opts.theme).toBe("dark")
  })
})
