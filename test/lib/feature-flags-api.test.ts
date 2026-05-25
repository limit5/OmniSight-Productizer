import { describe, expect, it } from "vitest"

import {
  DEFAULT_EFFECTIVE_FEATURE_FLAGS,
  FEATURE_FLAG_RELEASE_TIERS,
  normalizeEffectiveFeatureFlags,
} from "@/lib/api"

describe("feature flag API contracts", () => {
  it("keeps the frontend release tiers aligned to the backend enum", () => {
    expect(FEATURE_FLAG_RELEASE_TIERS).toEqual([
      "early_access",
      "staged",
      "ga",
    ])
  })

  it("normalizes effective flags as public booleans and fails closed", () => {
    expect(
      normalizeEffectiveFeatureFlags({
        flags: {
          "ui.release_train.enabled": true,
          "ui.new_navigation.enabled": "true",
          "internal.owner.leak": true,
        },
      }),
    ).toEqual({
      "ui.release_train.enabled": true,
      "ui.new_navigation.enabled": false,
      "ui.block_model.enabled": false,
    })

    expect(normalizeEffectiveFeatureFlags(null)).toEqual(
      DEFAULT_EFFECTIVE_FEATURE_FLAGS,
    )
  })
})
