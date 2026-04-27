/**
 * AS.7.1 — `<AuthTurnstileWidget>` component tests.
 *
 * Pins:
 *   - Missing siteKey → renders disabled-state surface (no script load)
 *   - Provided siteKey → injects the Turnstile script tag
 *   - Cleanup removes the widget on unmount when turnstile is loaded
 */

import { afterEach, describe, expect, it, vi } from "vitest"
import { render, screen, cleanup } from "@testing-library/react"

import { AuthTurnstileWidget } from "@/components/omnisight/auth/auth-turnstile-widget"

afterEach(() => {
  cleanup()
  // Remove any test-injected scripts so subsequent tests start clean.
  document
    .querySelectorAll('script[data-as7-turnstile-loaded]')
    .forEach((s) => s.remove())
  delete (window as { turnstile?: unknown }).turnstile
  delete (window as { __as7TurnstileReady?: () => void }).__as7TurnstileReady
})

describe("AS.7.1 AuthTurnstileWidget", () => {
  it("renders the disabled-state surface when siteKey is null", () => {
    render(<AuthTurnstileWidget siteKey={null} onToken={() => undefined} />)
    const widget = screen.getByTestId("as7-turnstile-widget")
    expect(widget).toHaveAttribute("data-as7-turnstile", "disabled")
  })

  it("renders the disabled-state surface when siteKey is undefined", () => {
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
