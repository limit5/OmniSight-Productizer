import React from "react"
import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, screen, waitFor } from "@testing-library/react"

import {
  FeatureFlagsProvider,
  FeatureGate,
} from "@/lib/feature-flags-context"
import type { EffectiveFeatureFlags } from "@/lib/api"

const enabledReleaseTrain: EffectiveFeatureFlags = {
  "ui.release_train.enabled": true,
  "ui.new_navigation.enabled": false,
}

const disabledReleaseTrain: EffectiveFeatureFlags = {
  "ui.release_train.enabled": false,
  "ui.new_navigation.enabled": false,
}

beforeEach(() => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(
      JSON.stringify({
        flags: disabledReleaseTrain,
      }),
      { status: 200 },
    ),
  )
})

describe("FeatureGate", () => {
  it("hides incomplete release-train UI when the flag is disabled", async () => {
    render(
      <FeatureFlagsProvider initialFlags={disabledReleaseTrain}>
        <FeatureGate flag="ui.release_train.enabled">
          <div data-testid="release-train-ui">release train</div>
        </FeatureGate>
      </FeatureFlagsProvider>,
    )

    expect(screen.queryByTestId("release-train-ui")).not.toBeInTheDocument()
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalledTimes(1))
  })

  it("uses SSR bootstrap flags before the client refresh completes", () => {
    render(
      <FeatureFlagsProvider initialFlags={enabledReleaseTrain}>
        <FeatureGate flag="ui.release_train.enabled">
          <div data-testid="release-train-ui">release train</div>
        </FeatureGate>
      </FeatureFlagsProvider>,
    )

    expect(screen.getByTestId("release-train-ui")).toBeInTheDocument()
  })
})
