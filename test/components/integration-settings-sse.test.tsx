/**
 * Q.3-SUB-5 (#297) — IntegrationSettings modal cross-device SSE refetch.
 *
 * Before Q.3-SUB-5 the modal only pulled fresh values on its
 * ``useEffect(() => { if (open) refetch(); }, [open])`` transition.
 * A passively-open modal on device A never saw device B's save until
 * the operator closed and re-opened it — the SharedKV mirror kept
 * the backend workers coherent but the UI didn't know about the
 * change. This suite locks the refetch contract now wired in
 * ``components/omnisight/integration-settings.tsx``:
 *
 *   1. On modal open the initial ``getSettings()`` + ``getProviders()``
 *      pair still fires (baseline behaviour preserved).
 *   2. While the modal is open, an ``integration.settings.updated``
 *      SSE event triggers a fresh ``getSettings()`` + ``getProviders()``
 *      call so the view re-renders against the merged value.
 *   3. Unrelated SSE events (``agent_update``, etc.) do NOT trigger
 *      refetches — they'd burn REST cycles without changing the
 *      modal's shape.
 *   4. Closing the modal tears down the SSE subscription so an open
 *      → close → event arrives sequence doesn't silently keep
 *      subscribing — the underlying EventSource is shared across
 *      the whole app and losing this unsubscribe would leak handlers.
 */

import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, waitFor } from "@testing-library/react"

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>()
  return {
    ...actual,
    getSettings: vi.fn(),
    getProviders: vi.fn(),
    subscribeEvents: vi.fn(),
    // Mutating helpers that the component imports at module load —
    // stubbed so the render doesn't hit real network.
    updateSettings: vi.fn(),
    testIntegration: vi.fn(),
    testGitForgeToken: vi.fn(),
    getGitTokenMap: vi.fn(),
    updateGitTokenMap: vi.fn(),
    getGitForgeSshPubkey: vi.fn(),
    verifyGerritMergerBot: vi.fn(),
    verifyGerritSubmitRule: vi.fn(),
    getGerritWebhookInfo: vi.fn(),
    generateGerritWebhookSecret: vi.fn(),
    finalizeGerritIntegration: vi.fn(),
  }
})

import * as api from "@/lib/api"
import { IntegrationSettings } from "@/components/omnisight/integration-settings"
import { primeSSE } from "../helpers/sse"

const mockedGetSettings = api.getSettings as unknown as ReturnType<typeof vi.fn>
const mockedGetProviders = api.getProviders as unknown as ReturnType<typeof vi.fn>
const mockedGetGitTokenMap = api.getGitTokenMap as unknown as ReturnType<typeof vi.fn>

beforeEach(() => {
  mockedGetSettings.mockReset()
  mockedGetProviders.mockReset()
  mockedGetSettings.mockResolvedValue({})
  mockedGetProviders.mockResolvedValue({ providers: [] })
  mockedGetGitTokenMap.mockReset()
  mockedGetGitTokenMap.mockResolvedValue({
    github: { instances: [] },
    gitlab: { instances: [] },
  })
  // subscribeEvents call count accumulates across tests because
  // ``vi.mock`` hoists a single vi.fn() for the whole file. Reset
  // between tests so "never called" assertions don't see prior
  // render's subscribe.
  ;(api.subscribeEvents as ReturnType<typeof vi.fn>).mockReset()
})


describe("IntegrationSettings — Q.3-SUB-5 SSE refetch", () => {
  it("performs the baseline getSettings + getProviders on modal open", async () => {
    primeSSE(api)
    render(<IntegrationSettings open={true} onClose={() => {}} />)

    await waitFor(() => {
      expect(mockedGetSettings).toHaveBeenCalledTimes(1)
      expect(mockedGetProviders).toHaveBeenCalledTimes(1)
    })
  })

  it("refetches getSettings + getProviders on integration.settings.updated", async () => {
    const sse = primeSSE(api)
    render(<IntegrationSettings open={true} onClose={() => {}} />)

    // Baseline fetch on open.
    await waitFor(() => {
      expect(mockedGetSettings).toHaveBeenCalledTimes(1)
      expect(mockedGetProviders).toHaveBeenCalledTimes(1)
    })

    // Simulate device B's save landing — the SSE push hits device A's
    // IntegrationSettings subscription, which must refetch.
    sse.emit({
      event: "integration.settings.updated",
      data: {
        fields_changed: ["gerrit_url", "gerrit_project"],
        timestamp: "2026-04-24T00:00:00",
      },
    })

    await waitFor(() => {
      expect(mockedGetSettings).toHaveBeenCalledTimes(2)
      expect(mockedGetProviders).toHaveBeenCalledTimes(2)
    })
  })

  it("ignores unrelated SSE events (no refetch thrash)", async () => {
    const sse = primeSSE(api)
    render(<IntegrationSettings open={true} onClose={() => {}} />)

    await waitFor(() => {
      expect(mockedGetSettings).toHaveBeenCalledTimes(1)
      expect(mockedGetProviders).toHaveBeenCalledTimes(1)
    })

    // Fan-in events that aren't ours — the modal must stay quiet so
    // the REST endpoints aren't hammered by unrelated engine chatter.
    sse.emit({
      event: "agent_update",
      data: {
        agent_id: "a1",
        status: "idle",
        thought_chain: "",
        timestamp: "2026-04-24T00:00:01",
      },
    })
    sse.emit({
      event: "heartbeat",
      data: { subscribers: 3 },
    })

    // A short wait so any missed fetch-on-other-events regression has
    // time to land before the assertion locks in.
    await new Promise(r => setTimeout(r, 20))
    expect(mockedGetSettings).toHaveBeenCalledTimes(1)
    expect(mockedGetProviders).toHaveBeenCalledTimes(1)
  })

  it("unsubscribes on modal close (no leaked handlers)", async () => {
    const sse = primeSSE(api)
    const { rerender } = render(
      <IntegrationSettings open={true} onClose={() => {}} />,
    )

    await waitFor(() => {
      expect(mockedGetSettings).toHaveBeenCalledTimes(1)
    })
    // The subscription handle's close() count stays 0 while the modal
    // is open — subscribeEvents has been called but the effect
    // cleanup has not yet run.
    expect(sse.closeCount()).toBe(0)

    rerender(<IntegrationSettings open={false} onClose={() => {}} />)

    // The effect cleanup fires on the ``open -> false`` transition,
    // which is the only path that tears down the SSE subscription.
    // Real-world `subscribeEvents` removes the registered listener
    // when the handle's ``close()`` runs; we assert the handle call
    // here (the primeSSE helper doesn't mutate the listener array
    // on close, so late-event assertions aren't meaningful against
    // this mock — but the real implementation does unsubscribe
    // correctly, which is what the test-helper's close-counter
    // contract proxies for).
    expect(sse.closeCount()).toBe(1)
  })

  it("does not subscribe while modal is closed", async () => {
    primeSSE(api)
    render(<IntegrationSettings open={false} onClose={() => {}} />)

    await new Promise(r => setTimeout(r, 20))
    // subscribeEvents stays uncalled — the SSE effect is gated on
    // ``open`` so closed modals don't hold a listener slot.
    expect(api.subscribeEvents).not.toHaveBeenCalled()
    expect(mockedGetSettings).not.toHaveBeenCalled()
    expect(mockedGetProviders).not.toHaveBeenCalled()
  })
})
