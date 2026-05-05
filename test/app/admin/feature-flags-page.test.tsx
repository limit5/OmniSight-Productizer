/**
 * WP.7.8 -- /admin/feature-flags page contract tests.
 *
 * Mirrors the /admin/tenants page test shape: all roles may inspect
 * rows, admin+ sessions can toggle, and toggle failures surface inline.
 */

import React from "react"
import { describe, expect, it, vi, beforeEach } from "vitest"
import {
  render,
  screen,
  fireEvent,
  waitFor,
  cleanup,
} from "@testing-library/react"

vi.mock("@/lib/auth-context", () => ({
  useAuth: vi.fn(),
}))

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>()
  return {
    ...actual,
    listFeatureFlags: vi.fn(),
    patchFeatureFlag: vi.fn(),
  }
})

import AdminFeatureFlagsPage from "@/app/admin/feature-flags/page"
import { useAuth } from "@/lib/auth-context"
import {
  listFeatureFlags,
  patchFeatureFlag,
  ApiError,
  type FeatureFlagRow,
} from "@/lib/api"

const mockedUseAuth = useAuth as unknown as ReturnType<typeof vi.fn>
const mockedList = listFeatureFlags as unknown as ReturnType<typeof vi.fn>
const mockedPatch = patchFeatureFlag as unknown as ReturnType<typeof vi.fn>

const sampleRows: FeatureFlagRow[] = [
  {
    flag_name: "wp.diff_validation.enabled",
    tier: "release",
    state: "enabled",
    expires_at: null,
    owner: "wp",
    created_at: "2026-05-05 00:00:00",
  },
  {
    flag_name: "ks.cmek.enabled",
    tier: "preview",
    state: "disabled",
    expires_at: "2026-06-01T00:00:00Z",
    owner: "ks",
    created_at: "2026-05-05 00:00:00",
  },
]

beforeEach(() => {
  vi.clearAllMocks()
  cleanup()
})

describe("/admin/feature-flags -- read-only roles", () => {
  it("lets viewer inspect flags but disables toggles", async () => {
    mockedUseAuth.mockReturnValue({
      user: {
        id: "u-viewer",
        email: "viewer@x.io",
        name: "Viewer",
        role: "viewer",
        enabled: true,
        tenant_id: "t-default",
      },
      authMode: "session",
      loading: false,
    })
    mockedList.mockResolvedValue({
      feature_flags: sampleRows,
      can_toggle: false,
    })

    render(<AdminFeatureFlagsPage />)
    await waitFor(() =>
      expect(
        screen.getByTestId("feature-flag-row-wp.diff_validation.enabled"),
      ).toBeInTheDocument(),
    )

    expect(screen.getByTestId("feature-flags-readonly")).toBeInTheDocument()
    expect(
      screen.getByTestId("feature-flag-toggle-wp.diff_validation.enabled"),
    ).toBeDisabled()
    expect(mockedPatch).not.toHaveBeenCalled()
  })
})

describe("/admin/feature-flags -- admin toggles", () => {
  beforeEach(() => {
    mockedUseAuth.mockReturnValue({
      user: {
        id: "u-admin",
        email: "admin@x.io",
        name: "Admin",
        role: "admin",
        enabled: true,
        tenant_id: "t-default",
      },
      authMode: "session",
      loading: false,
    })
  })

  it("renders rows and calls patchFeatureFlag with the opposite state", async () => {
    mockedList.mockResolvedValue({
      feature_flags: sampleRows,
      can_toggle: true,
    })
    mockedPatch.mockResolvedValue({
      feature_flag: {
        ...sampleRows[0],
        state: "disabled",
      },
    })

    render(<AdminFeatureFlagsPage />)
    await waitFor(() =>
      expect(
        screen.getByTestId("feature-flag-row-wp.diff_validation.enabled"),
      ).toBeInTheDocument(),
    )

    fireEvent.click(
      screen.getByTestId("feature-flag-toggle-wp.diff_validation.enabled"),
    )

    await waitFor(() => expect(mockedPatch).toHaveBeenCalledTimes(1))
    expect(mockedPatch).toHaveBeenCalledWith(
      "wp.diff_validation.enabled",
      "disabled",
    )
    await waitFor(() =>
      expect(
        screen.getByTestId("feature-flag-state-wp.diff_validation.enabled"),
      ).toHaveTextContent("disabled"),
    )
  })

  it("surfaces toggle failures inline on the flag row", async () => {
    mockedList.mockResolvedValue({
      feature_flags: sampleRows,
      can_toggle: true,
    })
    mockedPatch.mockRejectedValue(
      new ApiError({
        kind: "server",
        status: 500,
        body: '{"detail":"audit unavailable"}',
        parsed: { detail: "audit unavailable" },
        traceId: null,
        path: "/feature-flags/wp.diff_validation.enabled",
        method: "PATCH",
      }),
    )

    render(<AdminFeatureFlagsPage />)
    await waitFor(() =>
      expect(
        screen.getByTestId("feature-flag-row-wp.diff_validation.enabled"),
      ).toBeInTheDocument(),
    )

    fireEvent.click(
      screen.getByTestId("feature-flag-toggle-wp.diff_validation.enabled"),
    )

    await waitFor(() =>
      expect(
        screen.getByTestId("feature-flag-row-error-wp.diff_validation.enabled"),
      ).toBeInTheDocument(),
    )
    expect(
      screen.getByTestId("feature-flag-row-error-wp.diff_validation.enabled"),
    ).toHaveTextContent("audit unavailable")
  })
})
