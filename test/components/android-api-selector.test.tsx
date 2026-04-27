import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, screen, fireEvent } from "@testing-library/react"

import AndroidApiSelector, {
  ANDROID_API_LEVELS,
  ANDROID_API_LEVEL_VALUES,
  ANDROID_EMULATOR_PRESETS,
  ANDROID_EMULATOR_PRESET_IDS,
  ANDROID_LATEST_API_LEVEL,
  ANDROID_OLDEST_API_LEVEL,
  DEFAULT_ANDROID_API_SELECTION,
  androidApiLevelDef,
  androidDiskBreakdown,
  androidEmulatorPresetDef,
  clampMinApi,
  coerceAndroidApiSelection,
  estimateAndroidDiskBytes,
  formatAndroidDiskBytes,
  isAndroidApiLevel,
  isAndroidEmulatorPresetId,
  type AndroidApiSelection,
} from "@/components/omnisight/android-api-selector"

// ─── Pure helpers ────────────────────────────────────────────────────

describe("AndroidApiSelector — pure helpers", () => {
  it("ANDROID_API_LEVELS ships newest-first and locks the supported set", () => {
    expect(ANDROID_API_LEVEL_VALUES).toEqual([
      35, 34, 33, 32, 31, 30, 29, 28, 26, 24, 23,
    ])
    // Newest-first ordering matters because the dropdown's first
    // option doubles as the operator-default compile target.
    expect(ANDROID_LATEST_API_LEVEL).toBe(35)
    expect(ANDROID_OLDEST_API_LEVEL).toBe(23)
    // Each level carries a non-zero size triple so the disk estimate
    // never reports a phantom 0 B for a real install.
    for (const def of ANDROID_API_LEVELS) {
      expect(def.platformSizeBytes).toBeGreaterThan(0)
      expect(def.systemImageGmsBytes).toBeGreaterThan(0)
      expect(def.systemImageAospBytes).toBeGreaterThan(0)
      // GMS image is consistently larger than the AOSP image at the
      // same level (Play Services adds ~500 MB).
      expect(def.systemImageGmsBytes).toBeGreaterThan(
        def.systemImageAospBytes,
      )
    }
  })

  it("ANDROID_EMULATOR_PRESETS pins the canonical id set + 'none' tail", () => {
    expect(ANDROID_EMULATOR_PRESET_IDS).toEqual([
      "pixel-8",
      "pixel-6a",
      "pixel-tablet",
      "pixel-fold",
      "none",
    ])
    // 'none' must be the only preset that adds nothing to the form
    // factor (it's a "skip the system image" sentinel).
    const none = ANDROID_EMULATOR_PRESETS.find((p) => p.id === "none")
    expect(none).toBeDefined()
    expect(none?.formFactorDeltaBytes).toBe(0)
  })

  it("isAndroidApiLevel narrows correctly", () => {
    expect(isAndroidApiLevel(34)).toBe(true)
    expect(isAndroidApiLevel(99)).toBe(false)
    expect(isAndroidApiLevel("34")).toBe(false)
    expect(isAndroidApiLevel(null)).toBe(false)
    expect(isAndroidApiLevel(undefined)).toBe(false)
  })

  it("isAndroidEmulatorPresetId narrows correctly", () => {
    expect(isAndroidEmulatorPresetId("pixel-8")).toBe(true)
    expect(isAndroidEmulatorPresetId("none")).toBe(true)
    expect(isAndroidEmulatorPresetId("rtos")).toBe(false)
    expect(isAndroidEmulatorPresetId(7)).toBe(false)
    expect(isAndroidEmulatorPresetId(null)).toBe(false)
  })

  it("clampMinApi snaps min_api ≤ compile_target", () => {
    expect(clampMinApi(26, 34)).toBe(26)
    expect(clampMinApi(35, 30)).toBe(30)
    expect(clampMinApi(28, 28)).toBe(28)
  })

  it("coerceAndroidApiSelection drops invalid fields and falls back to default", () => {
    expect(coerceAndroidApiSelection(undefined)).toEqual(
      DEFAULT_ANDROID_API_SELECTION,
    )
    expect(coerceAndroidApiSelection(null)).toEqual(
      DEFAULT_ANDROID_API_SELECTION,
    )
    expect(coerceAndroidApiSelection("nope")).toEqual(
      DEFAULT_ANDROID_API_SELECTION,
    )
    // Bogus compile_target + bogus emulator_preset -> defaults.
    expect(
      coerceAndroidApiSelection({
        compile_target: 99,
        min_api: "abc",
        emulator_preset: "watch",
        google_play_services: "yes",
      }),
    ).toEqual(DEFAULT_ANDROID_API_SELECTION)
  })

  it("coerceAndroidApiSelection clamps min_api ≤ compile_target on rehydrate", () => {
    expect(
      coerceAndroidApiSelection({
        compile_target: 30,
        min_api: 34,
        emulator_preset: "pixel-tablet",
        google_play_services: false,
      }),
    ).toEqual({
      compile_target: 30,
      min_api: 30,
      emulator_preset: "pixel-tablet",
      google_play_services: false,
    })
  })

  it("androidApiLevelDef + androidEmulatorPresetDef look up canonical defs", () => {
    expect(androidApiLevelDef(34).versionName).toBe("Android 14")
    expect(androidEmulatorPresetDef("pixel-fold").label).toMatch(/Fold/)
  })

  it("formatAndroidDiskBytes mirrors the install-drawer cascade", () => {
    expect(formatAndroidDiskBytes(0)).toBe("0 B")
    expect(formatAndroidDiskBytes(-50)).toBe("0 B")
    expect(formatAndroidDiskBytes(NaN)).toBe("0 B")
    expect(formatAndroidDiskBytes(1024)).toBe("1.0 KB")
    expect(formatAndroidDiskBytes(1024 * 1024)).toBe("1.0 MB")
    expect(formatAndroidDiskBytes(1024 * 1024 * 1024)).toBe("1.0 GB")
    // ≥ 100 drops decimal so the chip stays readable on long lines.
    expect(formatAndroidDiskBytes(150 * 1024 * 1024)).toBe("150 MB")
  })

  it("androidDiskBreakdown skips system image when emulator preset is 'none'", () => {
    const lines = androidDiskBreakdown({
      compile_target: 34,
      min_api: 34,
      emulator_preset: "none",
      google_play_services: true,
    })
    const ids = lines.map((l) => l.id)
    expect(ids).toContain("platform-tools")
    expect(ids).toContain("compile-platform")
    expect(ids).toContain("build-tools")
    expect(ids).not.toContain("emulator-runtime")
    expect(ids).not.toContain("system-image")
    // No min-api-platform line because compile == min.
    expect(ids).not.toContain("min-api-platform")
  })

  it("androidDiskBreakdown adds a min-api-platform line when min ≠ compile", () => {
    const lines = androidDiskBreakdown({
      compile_target: 34,
      min_api: 26,
      emulator_preset: "none",
      google_play_services: false,
    })
    const ids = lines.map((l) => l.id)
    expect(ids).toContain("min-api-platform")
  })

  it("androidDiskBreakdown grows when GMS toggles on (emulator picked)", () => {
    const aosp = estimateAndroidDiskBytes({
      compile_target: 34,
      min_api: 26,
      emulator_preset: "pixel-8",
      google_play_services: false,
    })
    const gms = estimateAndroidDiskBytes({
      compile_target: 34,
      min_api: 26,
      emulator_preset: "pixel-8",
      google_play_services: true,
    })
    expect(gms).toBeGreaterThan(aosp)
  })

  it("estimateAndroidDiskBytes drops by the emulator-runtime + system image when preset 'none'", () => {
    const withEmulator = estimateAndroidDiskBytes({
      compile_target: 34,
      min_api: 34,
      emulator_preset: "pixel-8",
      google_play_services: true,
    })
    const withoutEmulator = estimateAndroidDiskBytes({
      compile_target: 34,
      min_api: 34,
      emulator_preset: "none",
      google_play_services: true,
    })
    // Skipping the emulator should save at least ~2 GB (system image
    // + emulator runtime).
    expect(withEmulator - withoutEmulator).toBeGreaterThan(1024 * 1024 * 1024)
  })
})

// ─── Component rendering + interaction ───────────────────────────────

describe("AndroidApiSelector — rendering + interaction", () => {
  let onChange: ReturnType<typeof vi.fn>

  beforeEach(() => {
    onChange = vi.fn()
  })

  it("renders the default selection (latest compile target, GMS-on, Pixel 8 preset)", () => {
    render(<AndroidApiSelector onChange={onChange} />)

    const root = screen.getByTestId("android-api-selector")
    expect(root).toHaveAttribute(
      "data-compile-target",
      String(ANDROID_LATEST_API_LEVEL),
    )
    expect(root).toHaveAttribute("data-min-api", "26")
    expect(root).toHaveAttribute("data-emulator-preset", "pixel-8")
    expect(root).toHaveAttribute("data-gms", "on")

    expect(
      screen.getByTestId("android-api-selector-emulator-preset-pixel-8"),
    ).toHaveAttribute("aria-checked", "true")
    expect(
      screen.getByTestId("android-api-selector-gms-on"),
    ).toHaveAttribute("aria-checked", "true")
  })

  it("changing the compile target fires onChange with the coerced payload", () => {
    render(<AndroidApiSelector onChange={onChange} />)

    fireEvent.change(
      screen.getByTestId("android-api-selector-compile-target"),
      { target: { value: "30" } },
    )

    expect(onChange).toHaveBeenCalledTimes(1)
    const next = onChange.mock.calls[0][0] as AndroidApiSelection
    expect(next.compile_target).toBe(30)
    // min_api was 26 in default, so it stays (still ≤ 30).
    expect(next.min_api).toBe(26)
  })

  it("dropping the compile target below the current min_api snaps min_api down", () => {
    render(
      <AndroidApiSelector
        onChange={onChange}
        value={{
          compile_target: 35,
          min_api: 33,
          emulator_preset: "pixel-8",
          google_play_services: true,
        }}
      />,
    )

    fireEvent.change(
      screen.getByTestId("android-api-selector-compile-target"),
      { target: { value: "30" } },
    )

    const next = onChange.mock.calls[0][0] as AndroidApiSelection
    expect(next.compile_target).toBe(30)
    expect(next.min_api).toBe(30)
  })

  it("min API dropdown only exposes levels ≤ compile target", () => {
    render(
      <AndroidApiSelector
        onChange={onChange}
        value={{
          compile_target: 28,
          min_api: 26,
          emulator_preset: "pixel-8",
          google_play_services: true,
        }}
      />,
    )

    const select = screen.getByTestId(
      "android-api-selector-min-api",
    ) as HTMLSelectElement
    const offered = Array.from(select.options).map((o) => Number(o.value))
    for (const v of offered) {
      expect(v).toBeLessThanOrEqual(28)
    }
    // 30 must NOT be offered when compile target is 28.
    expect(offered).not.toContain(30)
    // 28 / 26 must be offered.
    expect(offered).toContain(28)
    expect(offered).toContain(26)
  })

  it("clicking an emulator preset radio fires onChange with the picked id", () => {
    render(<AndroidApiSelector onChange={onChange} />)

    fireEvent.click(
      screen.getByTestId("android-api-selector-emulator-preset-pixel-tablet"),
    )

    expect(onChange).toHaveBeenCalledTimes(1)
    const next = onChange.mock.calls[0][0] as AndroidApiSelection
    expect(next.emulator_preset).toBe("pixel-tablet")
  })

  it("picking 'none' preset locks the GMS toggle and surfaces a hint", () => {
    render(
      <AndroidApiSelector
        onChange={onChange}
        value={{
          compile_target: 34,
          min_api: 26,
          emulator_preset: "none",
          google_play_services: true,
        }}
      />,
    )

    expect(screen.getByTestId("android-api-selector-gms-on")).toBeDisabled()
    expect(screen.getByTestId("android-api-selector-gms-off")).toBeDisabled()
    expect(
      screen.getByTestId("android-api-selector-gms-locked-hint"),
    ).toBeInTheDocument()

    // Disk breakdown collapses to no system-image / no emulator-runtime.
    expect(
      screen.queryByTestId("android-api-selector-disk-line-system-image"),
    ).not.toBeInTheDocument()
    expect(
      screen.queryByTestId("android-api-selector-disk-line-emulator-runtime"),
    ).not.toBeInTheDocument()
  })

  it("toggling the GMS pair fires onChange and flips the disk estimate", () => {
    const baseValue: AndroidApiSelection = {
      compile_target: 34,
      min_api: 26,
      emulator_preset: "pixel-8",
      google_play_services: true,
    }
    const { rerender } = render(
      <AndroidApiSelector onChange={onChange} value={baseValue} />,
    )

    const gmsBytes = Number(
      screen
        .getByTestId("android-api-selector")
        .getAttribute("data-disk-bytes") ?? 0,
    )

    fireEvent.click(screen.getByTestId("android-api-selector-gms-off"))
    expect(onChange).toHaveBeenCalledTimes(1)
    const flipped = onChange.mock.calls[0][0] as AndroidApiSelection
    expect(flipped.google_play_services).toBe(false)

    rerender(<AndroidApiSelector onChange={onChange} value={flipped} />)
    const aospBytes = Number(
      screen
        .getByTestId("android-api-selector")
        .getAttribute("data-disk-bytes") ?? 0,
    )
    expect(aospBytes).toBeLessThan(gmsBytes)
  })

  it("disk breakdown lists every breakdown line and the total matches the chip", () => {
    render(
      <AndroidApiSelector
        onChange={onChange}
        value={{
          compile_target: 34,
          min_api: 26,
          emulator_preset: "pixel-fold",
          google_play_services: true,
        }}
      />,
    )

    // All five lines render (compile/min differ → min-api line).
    expect(
      screen.getByTestId("android-api-selector-disk-line-platform-tools"),
    ).toBeInTheDocument()
    expect(
      screen.getByTestId("android-api-selector-disk-line-compile-platform"),
    ).toBeInTheDocument()
    expect(
      screen.getByTestId("android-api-selector-disk-line-build-tools"),
    ).toBeInTheDocument()
    expect(
      screen.getByTestId("android-api-selector-disk-line-min-api-platform"),
    ).toBeInTheDocument()
    expect(
      screen.getByTestId("android-api-selector-disk-line-emulator-runtime"),
    ).toBeInTheDocument()
    expect(
      screen.getByTestId("android-api-selector-disk-line-system-image"),
    ).toBeInTheDocument()

    // Header chip should match the breakdown sum.
    const root = screen.getByTestId("android-api-selector")
    const totalBytes = Number(root.getAttribute("data-disk-bytes"))
    const lineSum = Array.from(
      screen.getAllByTestId(/android-api-selector-disk-line-/),
    ).reduce((acc, el) => acc + Number(el.getAttribute("data-bytes") ?? 0), 0)
    expect(totalBytes).toBe(lineSum)
  })

  it("disabled prop blocks every interaction and emits no callback", () => {
    render(
      <AndroidApiSelector
        onChange={onChange}
        disabled
        value={DEFAULT_ANDROID_API_SELECTION}
      />,
    )

    expect(
      screen.getByTestId("android-api-selector-compile-target"),
    ).toBeDisabled()
    expect(
      screen.getByTestId("android-api-selector-min-api"),
    ).toBeDisabled()
    for (const id of ANDROID_EMULATOR_PRESET_IDS) {
      expect(
        screen.getByTestId(`android-api-selector-emulator-preset-${id}`),
      ).toBeDisabled()
    }
    expect(
      screen.getByTestId("android-api-selector-gms-on"),
    ).toBeDisabled()
    expect(
      screen.getByTestId("android-api-selector-gms-off"),
    ).toBeDisabled()

    // Even if a click somehow reaches a disabled button, no callback
    // fires (defence in depth on the dispatcher).
    fireEvent.click(
      screen.getByTestId("android-api-selector-emulator-preset-pixel-fold"),
    )
    fireEvent.change(
      screen.getByTestId("android-api-selector-compile-target"),
      { target: { value: "30" } },
    )
    expect(onChange).not.toHaveBeenCalled()
  })
})
