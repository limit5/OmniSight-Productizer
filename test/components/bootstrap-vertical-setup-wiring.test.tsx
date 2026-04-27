/**
 * BS.9.5 — VerticalSetupStep commit-flow wiring tests.
 *
 * Mounts ``<BootstrapPage />`` exactly the way the BS.9.2 suite does
 * (so the tests share the same router stub + ``getBootstrapStatus``
 * mock), drives the wizard down to the ``vertical_setup`` step, then
 * exercises the BS.9.5 contract:
 *
 *   1. Confirm picks → one ``createInstallJob(entryId, ...)`` call per
 *      selected vertical, using the canonical
 *      ``BOOTSTRAP_VERTICAL_PRIMARY_ENTRY`` map.
 *   2. Mobile pick → ``metadata.android_api`` rides on the
 *      ``createInstallJob`` payload AND on the
 *      ``bootstrapRecordVerticalSetup`` body.
 *   3. After every install enqueue resolves → ONE
 *      ``bootstrapRecordVerticalSetup`` call carrying the install_job
 *      ids, then the wizard pill flips green + advances.
 *   4. ``createInstallJob`` failure surfaces an inline error banner
 *      and the pill stays pending so the operator can retry.
 *   5. ``bootstrapRecordVerticalSetup`` failure surfaces an inline
 *      error banner and the pill stays pending (the install jobs are
 *      already enqueued + visible in the BS.7 drawer; the operator
 *      sees the failure and can hit Confirm again).
 *   6. Skip path remains client-only (no
 *      ``createInstallJob`` / ``bootstrapRecordVerticalSetup``
 *      side-effect — preserves the BS.9.2 contract).
 *   7. ``AndroidApiSelector`` only renders inside the step body when
 *      the Mobile vertical is currently selected.
 *
 * The BS.7 install-progress drawer is mounted globally inside
 * ``components/providers.tsx`` via ``<InstallProgressDrawerLive />``;
 * tests do NOT need to mount it explicitly — the ``createInstallJob``
 * mock simply asserts the POST was fired and the install drawer's
 * SSE subscription wakes up on its own in production.
 */

import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, screen, fireEvent, waitFor } from "@testing-library/react"

const routerReplace = vi.fn()
const stableRouter = { replace: routerReplace, push: vi.fn(), back: vi.fn() }

vi.mock("next/navigation", () => ({
  useRouter: () => stableRouter,
}))

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>()
  return {
    ...actual,
    getBootstrapStatus: vi.fn(),
    finalizeBootstrap: vi.fn(),
    bootstrapSetAdminPassword: vi.fn(),
    bootstrapLlmProvision: vi.fn(),
    bootstrapDetectOllama: vi.fn(),
    bootstrapCfTunnelSkip: vi.fn(),
    bootstrapParallelHealthCheck: vi.fn(),
    bootstrapSmokeSubset: vi.fn(),
    bootstrapStartServices: vi.fn(),
    testGitForgeToken: vi.fn(),
    updateSettings: vi.fn(),
    // BS.9.5 — wire the commit-flow side-effects:
    createInstallJob: vi.fn(),
    bootstrapRecordVerticalSetup: vi.fn(),
  }
})

vi.mock("@/components/omnisight/cloudflare-tunnel-setup", () => ({
  default: ({ open, onClose }: { open: boolean; onClose: () => void }) =>
    open ? (
      <div data-testid="cf-tunnel-modal-stub">
        <button data-testid="cf-tunnel-modal-close" onClick={onClose}>
          close
        </button>
      </div>
    ) : null,
}))

import BootstrapPage from "@/app/bootstrap/page"
import * as api from "@/lib/api"
import type { InstallJob } from "@/lib/api"

const mockedGetStatus = api.getBootstrapStatus as unknown as ReturnType<
  typeof vi.fn
>
const mockedCreateInstallJob = api.createInstallJob as unknown as ReturnType<
  typeof vi.fn
>
const mockedRecordVerticalSetup =
  api.bootstrapRecordVerticalSetup as unknown as ReturnType<typeof vi.fn>
const mockedParallelHealth = api.bootstrapParallelHealthCheck as unknown as ReturnType<
  typeof vi.fn
>
const mockedDetectOllama = api.bootstrapDetectOllama as unknown as ReturnType<
  typeof vi.fn
>

const redStatus = {
  status: {
    admin_password_default: true,
    llm_provider_configured: false,
    cf_tunnel_configured: false,
    smoke_passed: false,
  },
  all_green: false,
  finalized: false,
  missing_steps: [
    "admin_password_set",
    "llm_provider_configured",
    "cf_tunnel_configured",
    "smoke_passed",
  ],
}

function fakeJob(id: string, entryId: string): InstallJob {
  return {
    id,
    tenant_id: "t-default",
    entry_id: entryId,
    state: "queued",
    idempotency_key: "x".repeat(20),
    sidecar_id: null,
    protocol_version: 1,
    bytes_done: 0,
    bytes_total: null,
    eta_seconds: null,
    log_tail: "",
    result_json: null,
    error_reason: null,
    pep_decision_id: null,
    requested_by: "wizard",
    queued_at: "2026-04-27T00:00:00Z",
    claimed_at: null,
    started_at: null,
    completed_at: null,
  }
}

async function openVerticalSetupStep() {
  await waitFor(() => {
    expect(
      screen.getByTestId("bootstrap-step-vertical_setup"),
    ).toBeInTheDocument()
  })
  fireEvent.click(screen.getByTestId("bootstrap-step-vertical_setup"))
  await waitFor(() => {
    expect(
      screen.getByTestId("bootstrap-vertical-setup-step"),
    ).toBeInTheDocument()
  })
}

describe("BS.9.5 — VerticalSetupStep commit flow", () => {
  beforeEach(() => {
    routerReplace.mockClear()
    mockedGetStatus.mockReset()
    mockedCreateInstallJob.mockReset()
    mockedRecordVerticalSetup.mockReset()
    mockedDetectOllama.mockResolvedValue({
      reachable: false,
      base_url: "http://localhost:11434",
      latency_ms: 0,
      models: [],
      kind: "network_unreachable",
      detail: "probe not wired in tests",
    })
    mockedParallelHealth.mockReset()
    mockedParallelHealth.mockResolvedValue({
      all_green: true,
      elapsed_ms: 12,
      backend: { ok: true, status: "green", detail: null, latency_ms: 5 },
      frontend: { ok: true, status: "green", detail: null, latency_ms: 7 },
      db_migration: {
        ok: true,
        status: "green",
        detail: "5 invariants present",
        latency_ms: 1,
      },
      cf_tunnel: {
        ok: true,
        status: "skipped",
        detail: "operator skipped Step 3 (LAN-only)",
        latency_ms: 0,
      },
    })
    mockedGetStatus.mockResolvedValue(redStatus)
  })

  it("AndroidApiSelector is hidden until the Mobile vertical is checked", async () => {
    render(<BootstrapPage />)
    await openVerticalSetupStep()

    expect(
      screen.queryByTestId("bootstrap-vertical-setup-android-block"),
    ).toBeNull()

    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-mobile"))
    await waitFor(() => {
      expect(
        screen.getByTestId("bootstrap-vertical-setup-android-block"),
      ).toBeInTheDocument()
    })

    // Toggling Mobile back off hides the selector again.
    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-mobile"))
    await waitFor(() => {
      expect(
        screen.queryByTestId("bootstrap-vertical-setup-android-block"),
      ).toBeNull()
    })
  })

  it("Confirm picks fires one createInstallJob per selected vertical (canonical entry_ids)", async () => {
    mockedCreateInstallJob
      .mockResolvedValueOnce(fakeJob("ij-mobile-1", "android-sdk-platform-tools"))
      .mockResolvedValueOnce(fakeJob("ij-web-1", "nodejs-lts-20"))
    mockedRecordVerticalSetup.mockResolvedValue({
      status: "committed",
      verticals_selected: ["mobile", "web"],
      install_job_ids: ["ij-mobile-1", "ij-web-1"],
    })

    render(<BootstrapPage />)
    await openVerticalSetupStep()

    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-mobile"))
    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-web"))
    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-confirm"))

    await waitFor(() => {
      expect(mockedCreateInstallJob).toHaveBeenCalledTimes(2)
    })
    // Mobile first, web second — canonical order from BOOTSTRAP_VERTICALS.
    expect(mockedCreateInstallJob.mock.calls[0]![0]).toBe(
      "android-sdk-platform-tools",
    )
    expect(mockedCreateInstallJob.mock.calls[1]![0]).toBe("nodejs-lts-20")
  })

  it("Mobile pick rides android_api on both createInstallJob metadata + recordVerticalSetup body", async () => {
    mockedCreateInstallJob.mockResolvedValueOnce(
      fakeJob("ij-mobile-1", "android-sdk-platform-tools"),
    )
    mockedRecordVerticalSetup.mockResolvedValue({
      status: "committed",
      verticals_selected: ["mobile"],
      install_job_ids: ["ij-mobile-1"],
    })

    render(<BootstrapPage />)
    await openVerticalSetupStep()

    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-mobile"))
    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-confirm"))

    await waitFor(() => {
      expect(mockedRecordVerticalSetup).toHaveBeenCalledTimes(1)
    })

    const callArgs = mockedCreateInstallJob.mock.calls[0]!
    expect(callArgs[0]).toBe("android-sdk-platform-tools")
    const opts = callArgs[1] as { metadata: Record<string, unknown> }
    expect(opts.metadata).toMatchObject({
      vertical: "mobile",
      source: "bootstrap_wizard",
      android_api: {
        compile_target: 35,
        min_api: 26,
        emulator_preset: "pixel-8",
        google_play_services: true,
      },
    })

    const recordBody = mockedRecordVerticalSetup.mock.calls[0]![0] as {
      verticals_selected: string[]
      install_job_ids: string[]
      android_api: {
        compile_target: number
        min_api: number
        emulator_preset: string
        google_play_services: boolean
      } | null
    }
    expect(recordBody.verticals_selected).toEqual(["mobile"])
    expect(recordBody.install_job_ids).toEqual(["ij-mobile-1"])
    expect(recordBody.android_api).toEqual({
      compile_target: 35,
      min_api: 26,
      emulator_preset: "pixel-8",
      google_play_services: true,
    })
  })

  it("non-Mobile pick records android_api=null + omits android_api from createInstallJob metadata", async () => {
    mockedCreateInstallJob.mockResolvedValueOnce(
      fakeJob("ij-web-1", "nodejs-lts-20"),
    )
    mockedRecordVerticalSetup.mockResolvedValue({
      status: "committed",
      verticals_selected: ["web"],
      install_job_ids: ["ij-web-1"],
    })

    render(<BootstrapPage />)
    await openVerticalSetupStep()

    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-web"))
    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-confirm"))

    await waitFor(() => {
      expect(mockedRecordVerticalSetup).toHaveBeenCalledTimes(1)
    })

    const opts = mockedCreateInstallJob.mock.calls[0]![1] as {
      metadata: Record<string, unknown>
    }
    expect(opts.metadata.android_api).toBeUndefined()

    const recordBody = mockedRecordVerticalSetup.mock.calls[0]![0] as {
      android_api: unknown
    }
    expect(recordBody.android_api).toBeNull()
  })

  it("after successful commit pill flips to green + cursor advances to next step", async () => {
    mockedCreateInstallJob.mockResolvedValueOnce(
      fakeJob("ij-software-1", "python-uv"),
    )
    mockedRecordVerticalSetup.mockResolvedValue({
      status: "committed",
      verticals_selected: ["software"],
      install_job_ids: ["ij-software-1"],
    })

    render(<BootstrapPage />)
    await openVerticalSetupStep()

    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-software"))
    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-confirm"))

    await waitFor(() => {
      expect(
        screen.getByTestId("bootstrap-step-vertical_setup"),
      ).toHaveAttribute("data-state", "green")
    })
    // Cursor lands on services_ready (the next step in STEPS).
    await waitFor(() => {
      expect(
        screen.getByTestId("bootstrap-step-services_ready"),
      ).toHaveAttribute("aria-current", "step")
    })
  })

  it("createInstallJob failure surfaces inline error banner and keeps pill pending", async () => {
    mockedCreateInstallJob.mockRejectedValueOnce(
      new Error("PEP HOLD denied — operator rejected install"),
    )

    render(<BootstrapPage />)
    await openVerticalSetupStep()

    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-software"))
    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-confirm"))

    await waitFor(() => {
      expect(
        screen.getByTestId("bootstrap-vertical-setup-error"),
      ).toBeInTheDocument()
    })
    expect(
      screen.getByTestId("bootstrap-vertical-setup-error"),
    ).toHaveTextContent(/PEP HOLD denied/)

    // The vertical_setup pill must NOT be green (it stays "active" while
    // the operator is looking at the open step body); recordVerticalSetup
    // must not have been called once createInstallJob threw.
    expect(
      screen.getByTestId("bootstrap-step-vertical_setup"),
    ).not.toHaveAttribute("data-state", "green")
    expect(mockedRecordVerticalSetup).not.toHaveBeenCalled()
  })

  it("bootstrapRecordVerticalSetup failure surfaces inline error banner + leaves pill pending", async () => {
    mockedCreateInstallJob.mockResolvedValueOnce(
      fakeJob("ij-software-1", "python-uv"),
    )
    mockedRecordVerticalSetup.mockRejectedValueOnce(
      new Error("PG record_bootstrap_step disconnected"),
    )

    render(<BootstrapPage />)
    await openVerticalSetupStep()

    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-software"))
    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-confirm"))

    await waitFor(() => {
      expect(
        screen.getByTestId("bootstrap-vertical-setup-error"),
      ).toBeInTheDocument()
    })
    expect(
      screen.getByTestId("bootstrap-vertical-setup-error"),
    ).toHaveTextContent(/Recording vertical setup failed/)
    // Install job WAS enqueued before the record failed — that's the
    // commit semantics the BS.7 drawer relies on (the install row is
    // already alive in PG; operator can hit Confirm again to retry
    // just the record step).
    expect(mockedCreateInstallJob).toHaveBeenCalledTimes(1)
    expect(
      screen.getByTestId("bootstrap-step-vertical_setup"),
    ).not.toHaveAttribute("data-state", "green")
  })

  it("Skip path remains client-only — no createInstallJob / record call", async () => {
    render(<BootstrapPage />)
    await openVerticalSetupStep()

    fireEvent.click(screen.getByTestId("bootstrap-vertical-setup-skip"))

    await waitFor(() => {
      expect(
        screen.getByTestId("bootstrap-step-vertical_setup"),
      ).toHaveAttribute("data-state", "green")
    })
    expect(mockedCreateInstallJob).not.toHaveBeenCalled()
    expect(mockedRecordVerticalSetup).not.toHaveBeenCalled()
  })

  it("emits canonical-ordered verticals_selected even when the operator picks out of order", async () => {
    mockedCreateInstallJob
      .mockResolvedValueOnce(fakeJob("ij-mobile-1", "android-sdk-platform-tools"))
      .mockResolvedValueOnce(fakeJob("ij-embedded-1", "espressif-esp-idf-v5"))
      .mockResolvedValueOnce(fakeJob("ij-web-1", "nodejs-lts-20"))
    mockedRecordVerticalSetup.mockResolvedValue({
      status: "committed",
      verticals_selected: ["mobile", "embedded", "web"],
      install_job_ids: ["ij-mobile-1", "ij-embedded-1", "ij-web-1"],
    })

    render(<BootstrapPage />)
    await openVerticalSetupStep()

    // Click order: web → embedded → mobile (deliberately reversed).
    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-web"))
    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-embedded"))
    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-mobile"))
    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-confirm"))

    await waitFor(() => {
      expect(mockedRecordVerticalSetup).toHaveBeenCalledTimes(1)
    })
    const recordBody = mockedRecordVerticalSetup.mock.calls[0]![0] as {
      verticals_selected: string[]
      install_job_ids: string[]
    }
    expect(recordBody.verticals_selected).toEqual([
      "mobile",
      "embedded",
      "web",
    ])
    // createInstallJob runs in the canonical order too — Mobile first.
    expect(mockedCreateInstallJob.mock.calls[0]![0]).toBe(
      "android-sdk-platform-tools",
    )
    expect(mockedCreateInstallJob.mock.calls[1]![0]).toBe(
      "espressif-esp-idf-v5",
    )
    expect(mockedCreateInstallJob.mock.calls[2]![0]).toBe("nodejs-lts-20")
  })

  it("Confirm with the picker disabled (busy state) does not double-fire", async () => {
    // First call is held until we resolve it, so the picker stays in
    // busy state while we attempt a second click.
    let resolveFirst: ((j: InstallJob) => void) | undefined
    mockedCreateInstallJob.mockImplementationOnce(
      () =>
        new Promise<InstallJob>((resolve) => {
          resolveFirst = resolve
        }),
    )
    mockedRecordVerticalSetup.mockResolvedValue({
      status: "committed",
      verticals_selected: ["software"],
      install_job_ids: ["ij-software-1"],
    })

    render(<BootstrapPage />)
    await openVerticalSetupStep()

    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-software"))
    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-confirm"))

    // Busy banner appears; Confirm button reports disabled.
    await waitFor(() => {
      expect(
        screen.getByTestId("bootstrap-vertical-setup-busy"),
      ).toBeInTheDocument()
    })
    expect(
      screen.getByTestId("bootstrap-vertical-pick-confirm"),
    ).toBeDisabled()

    // Try clicking Confirm again — must not fire a second call.
    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-confirm"))
    expect(mockedCreateInstallJob).toHaveBeenCalledTimes(1)

    // Resolve the in-flight install + finish the commit flow cleanly
    // so the test does not leak a pending promise.
    resolveFirst?.(fakeJob("ij-software-1", "python-uv"))
    await waitFor(() => {
      expect(
        screen.getByTestId("bootstrap-step-vertical_setup"),
      ).toHaveAttribute("data-state", "green")
    })
  })
})
