import React from "react"
import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, screen, waitFor } from "@testing-library/react"

import {
  DEFAULT_EFFECTIVE_FEATURE_FLAGS,
  PUBLIC_EFFECTIVE_FEATURE_FLAGS,
  normalizeEffectiveFeatureFlags,
  type EffectiveFeatureFlags,
  type EffectiveFeatureFlagsResponse,
} from "@/lib/api"
import {
  FeatureFlagsProvider,
  FeatureGate,
} from "@/lib/feature-flags-context"
import { loadInitialEffectiveFeatureFlags } from "@/lib/feature-flags-ssr"
import contract from "@/test/fixtures/dark-ship-effective-flags.json"

// RT-15c (OP-1595) -- dark-ship verified end-to-end (frontend half).
//
// The backend half (backend/tests/test_feature_flags_dark_ship_e2e.py)
// proves the REAL effective-flags endpoint emits each phase's
// `endpoint_payload`. This suite proves the REAL frontend turns those
// SAME payloads -- read from the shared golden contract fixture -- into
// the documented UI visibility, through the production
// normalizer / SSR bootstrap / provider / FeatureGate. The fixture is
// the single source of truth for the wire contract across both stacks;
// if either side drifts, one suite goes red. No new provider code.

const FLAG = contract.flag_under_test as "ui.release_train.enabled"

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), { status })
}

// A minimal next/headers-style reader for the SSR bootstrap.
function fakeHeaders(entries: Record<string, string>) {
  return { get: (name: string) => entries[name.toLowerCase()] ?? null }
}

beforeEach(() => {
  vi.restoreAllMocks()
})

// ─── contract parity: the FE allow-list IS the shared contract ──────

describe("dark-ship wire contract parity", () => {
  it("frontend public flags match the golden contract public_flags", () => {
    expect([...PUBLIC_EFFECTIVE_FEATURE_FLAGS].sort()).toEqual(
      [...contract.public_flags].sort(),
    )
  })

  it("default (no payload) is the all-dark posture", () => {
    expect(normalizeEffectiveFeatureFlags(null)).toEqual(
      DEFAULT_EFFECTIVE_FEATURE_FLAGS,
    )
    expect(DEFAULT_EFFECTIVE_FEATURE_FLAGS[FLAG]).toBe(false)
  })
})

// ─── the lifecycle: each backend payload -> the documented UI state ─

describe("dark-ship lifecycle: gated UI tracks the backend payload", () => {
  for (const phaseName of ["dark", "ga"] as const) {
    const phase = contract.phases[phaseName]
    const payload = phase.endpoint_payload as EffectiveFeatureFlagsResponse
    const visible = phase.release_train_ui_visible

    it(`${phaseName}: release-train UI ${visible ? "shows" : "hides"} for the contract payload`, async () => {
      // The client-side refresh-on-mount fetch returns the SAME phase
      // payload, so the steady state matches the SSR-bootstrapped state.
      const fetchSpy = vi
        .spyOn(globalThis, "fetch")
        .mockResolvedValue(jsonResponse(payload))

      const initialFlags = normalizeEffectiveFeatureFlags(payload)
      render(
        <FeatureFlagsProvider initialFlags={initialFlags}>
          <FeatureGate flag={FLAG}>
            <div data-testid="release-train-ui">release train</div>
          </FeatureGate>
        </FeatureFlagsProvider>,
      )

      await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(1))
      const probe = screen.queryByTestId("release-train-ui")
      if (visible) {
        expect(probe).toBeInTheDocument()
      } else {
        expect(probe).not.toBeInTheDocument()
      }
    })
  }

  it("outage: a 200 all-false payload re-darkens the gated UI", async () => {
    const phase = contract.phases.outage
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse(phase.endpoint_payload),
    )

    render(
      <FeatureFlagsProvider
        initialFlags={normalizeEffectiveFeatureFlags(
          phase.endpoint_payload as EffectiveFeatureFlagsResponse,
        )}
      >
        <FeatureGate flag={FLAG}>
          <div data-testid="release-train-ui">release train</div>
        </FeatureGate>
      </FeatureFlagsProvider>,
    )

    await waitFor(() =>
      expect(screen.queryByTestId("release-train-ui")).not.toBeInTheDocument(),
    )
  })
})

// ─── SSR bootstrap consumes the same wire payloads ─────────────────

describe("SSR bootstrap consumes the contract payloads", () => {
  it("ga: bootstrap returns the flag enabled and pre-renders it visible", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse(contract.phases.ga.endpoint_payload))

    const bootstrapped = await loadInitialEffectiveFeatureFlags(
      fakeHeaders({ host: "prod.omnisight.app", cookie: "session=abc" }),
    )

    expect(fetchSpy).toHaveBeenCalledTimes(1)
    expect(bootstrapped).toEqual(
      normalizeEffectiveFeatureFlags(
        contract.phases.ga.endpoint_payload as EffectiveFeatureFlagsResponse,
      ),
    )
    expect(bootstrapped[FLAG]).toBe(true)
  })

  it("dark: a non-200 from the flag service bootstraps fully dark", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ detail: "down" }, 503),
    )

    const bootstrapped = await loadInitialEffectiveFeatureFlags(
      fakeHeaders({ host: "prod.omnisight.app" }),
    )

    expect(bootstrapped).toEqual(DEFAULT_EFFECTIVE_FEATURE_FLAGS)
    expect(bootstrapped[FLAG]).toBe(false)
  })
})

// ─── leak guard: hostile/legacy keys never reach the gated UI ───────

describe("normalizer drops non-allow-listed keys from the wire", () => {
  it("strips internal keys and coerces non-booleans before gating", () => {
    const leak = contract.leak_guard
    const normalized = normalizeEffectiveFeatureFlags(
      leak.raw_endpoint_payload as EffectiveFeatureFlagsResponse,
    )

    expect(normalized).toEqual(leak.normalized as EffectiveFeatureFlags)
    expect(Object.keys(normalized).sort()).toEqual(
      [...contract.public_flags].sort(),
    )
    // The string "true" must NOT be treated as enabled.
    expect(normalized["ui.new_navigation.enabled"]).toBe(false)
  })
})
