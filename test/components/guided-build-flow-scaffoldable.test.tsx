import { render, screen, waitFor } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"

vi.mock("@/lib/api", () => ({
  listScaffoldableSkills: vi.fn(),
  submitDag: vi.fn(),
  validateDag: vi.fn(),
}))

import { GuidedBuildFlow } from "@/components/omnisight/guided-build-flow"
import {
  listScaffoldableSkills,
  validateDag,
  type ParsedSpec,
} from "@/lib/api"

const parsedSpec: ParsedSpec = {
  project_type: { value: "mobile_app", confidence: 0.9 },
  runtime_model: { value: "interactive", confidence: 0.9 },
  target_arch: { value: "arm64", confidence: 0.9 },
  target_os: { value: "ios", confidence: 0.9 },
  framework: { value: "swift", confidence: 0.9 },
  persistence: { value: "none", confidence: 0.9 },
  deploy_target: { value: "device", confidence: 0.9 },
  hardware_required: { value: "yes", confidence: 0.9 },
  raw_text: "Build an iOS map AR app and compare Case packs",
  conflicts: [],
}

describe("GuidedBuildFlow scaffoldable packs", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(listScaffoldableSkills as ReturnType<typeof vi.fn>).mockResolvedValue({
      items: [
        "android-rtsp-onvif-client",
        "windows-uvc-host",
        "ios-map-ar",
      ],
      count: 3,
    })
    ;(validateDag as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      stage: "semantic",
      errors: [],
    })
  })

  it("surfaces scaffoldable Case packs in the post-spec kickoff", async () => {
    render(<GuidedBuildFlow spec={parsedSpec} onOpenEditor={vi.fn()} />)

    await waitFor(() => expect(listScaffoldableSkills).toHaveBeenCalled())

    expect(await screen.findByText("Android RTSP ONVIF Client")).toBeInTheDocument()
    expect(screen.getByText("Windows UVC Host")).toBeInTheDocument()
    expect(screen.getAllByText("IOS Map AR").length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText("3 available")).toBeInTheDocument()
  })
})
