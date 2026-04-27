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

// ─── BS.9.6 — deepening test deck ────────────────────────────────────
//
// BS.9.4 shipped 22 cases pinning the level catalog, the four pure
// helpers, and the basic dropdown / preset radio / GMS toggle paths.
// BS.9.5 then bolted the selector inside ``VerticalSetupStep`` (Mobile
// vertical sub-step) without touching this file. BS.9.6 fills the
// gaps below the BS.9.4/9.5 line:
//   - drift guards on the ``ANDROID_API_LEVELS`` /
//     ``ANDROID_EMULATOR_PRESETS`` catalogs (newest-first ordering,
//     form-factor delta sign rules, complete operator copy);
//   - explicit dropdown content invariants (compile target options
//     count, min API options shrink with compile drop, summary footer
//     mirrors all four selection facets);
//   - integration-shape invariants the BS.9.5 batch enqueue depends
//     on (onChange does not double-fire on rerender, clamp invariant
//     holds across a sequence of edits, disk total chip mirrors the
//     breakdown footer exactly).
describe("AndroidApiSelector — BS.9.6 deepening", () => {
  let onChange: ReturnType<typeof vi.fn>

  beforeEach(() => {
    onChange = vi.fn()
  })

  it("ANDROID_API_LEVELS levels are strictly descending and platform sizes monotone non-decreasing (drift guard)", () => {
    // Level integers must be sorted strictly newest-first so the
    // compile-target dropdown's first option doubles as the
    // operator-default. A drift in either direction (re-order,
    // duplicate level, off-by-one in the catalog seed) would silently
    // break the "first option = latest stable" invariant the parent
    // relies on.
    for (let i = 1; i < ANDROID_API_LEVELS.length; i++) {
      expect(ANDROID_API_LEVELS[i].level).toBeLessThan(
        ANDROID_API_LEVELS[i - 1].level,
      )
    }
    // Platform sizes monotone non-decreasing in level — i.e., older
    // SDK platforms should not exceed newer ones (Google's catalog
    // pattern). A future refactor that mis-types a row would surface
    // here rather than at "operator sees 250 MB for Android 6.0 but
    // 95 MB for Android 14".
    for (let i = 1; i < ANDROID_API_LEVELS.length; i++) {
      expect(ANDROID_API_LEVELS[i].platformSizeBytes).toBeLessThanOrEqual(
        ANDROID_API_LEVELS[i - 1].platformSizeBytes,
      )
    }
    // Every entry has full operator copy (versionName / codename /
    // releasedYear) so the dropdown label and hint never render
    // empty.
    for (const def of ANDROID_API_LEVELS) {
      expect(def.versionName.length).toBeGreaterThan(0)
      expect(def.codename.length).toBeGreaterThan(0)
      expect(def.releasedYear).toBeGreaterThan(2000)
      expect(def.releasedYear).toBeLessThan(2100)
    }
  })

  it("ANDROID_EMULATOR_PRESETS form-factor deltas have the right sign per preset (drift guard)", () => {
    // pixel-8 is the baseline (delta = 0); pixel-6a is mid-tier so
    // smaller (negative delta); tablet + foldable carry larger system
    // images (positive delta); 'none' is the skip-the-image sentinel
    // (delta = 0, validated separately in the BS.9.4 pure-helper
    // test). A future refactor that flipped the tablet vs phone
    // bytes would mis-report the disk estimate by ~150 MB on every
    // tablet pick.
    const byId = new Map(ANDROID_EMULATOR_PRESETS.map((p) => [p.id, p]))
    expect(byId.get("pixel-8")?.formFactorDeltaBytes).toBe(0)
    expect(byId.get("none")?.formFactorDeltaBytes).toBe(0)
    expect(byId.get("pixel-6a")?.formFactorDeltaBytes ?? 0).toBeLessThan(0)
    expect(byId.get("pixel-tablet")?.formFactorDeltaBytes ?? 0).toBeGreaterThan(
      0,
    )
    expect(byId.get("pixel-fold")?.formFactorDeltaBytes ?? 0).toBeGreaterThan(0)
    // Foldable should run heavier than tablet — both screens shipped.
    expect(
      (byId.get("pixel-fold")?.formFactorDeltaBytes ?? 0) >=
        (byId.get("pixel-tablet")?.formFactorDeltaBytes ?? 0),
    ).toBe(true)

    // Every preset has full operator copy + an icon slot.
    for (const p of ANDROID_EMULATOR_PRESETS) {
      expect(p.label.length).toBeGreaterThan(0)
      expect(p.hint.length).toBeGreaterThan(0)
      expect(p.icon).toBeTruthy()
    }
  })

  it("compile target dropdown lists exactly the supported set in newest-first order", () => {
    // BS.9.5's batch enqueue / backend 422 guard validate the picked
    // compile target against ``_ANDROID_API_LEVELS`` — the dropdown
    // must never expose an unsupported option. Lock that shape here
    // so a future option-filter / sort refactor can't silently leak
    // levels.
    render(<AndroidApiSelector onChange={onChange} />)
    const select = screen.getByTestId(
      "android-api-selector-compile-target",
    ) as HTMLSelectElement
    const offered = Array.from(select.options).map((o) => Number(o.value))
    expect(offered).toEqual(ANDROID_API_LEVELS.map((d) => d.level))
    expect(offered.length).toBe(11)
    expect(offered[0]).toBe(ANDROID_LATEST_API_LEVEL)
    expect(offered[offered.length - 1]).toBe(ANDROID_OLDEST_API_LEVEL)
  })

  it("min API dropdown option count shrinks dynamically with the compile target", () => {
    // The dropdown filter is a render-time invariant — the BS.9.5
    // backend 422 guard rejects min > compile so a stale dropdown
    // option would mis-route an operator pick into a 422 error after
    // the wizard already advanced. Pin the count for a couple of
    // anchor compile targets to surface filter regressions early.
    const totalLevels = ANDROID_API_LEVELS.length
    const expectedAt = (compile: number) =>
      ANDROID_API_LEVELS.filter((d) => d.level <= compile).length
    const { rerender } = render(
      <AndroidApiSelector
        onChange={onChange}
        value={{
          compile_target: 35,
          min_api: 26,
          emulator_preset: "pixel-8",
          google_play_services: true,
        }}
      />,
    )
    let select = screen.getByTestId(
      "android-api-selector-min-api",
    ) as HTMLSelectElement
    expect(select.options.length).toBe(totalLevels)
    expect(select.options.length).toBe(expectedAt(35))

    rerender(
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
    select = screen.getByTestId(
      "android-api-selector-min-api",
    ) as HTMLSelectElement
    expect(select.options.length).toBe(expectedAt(28))
    // 30 / 31 / 32 / 33 / 34 / 35 are all dropped.
    const offered = Array.from(select.options).map((o) => Number(o.value))
    for (const blocked of [30, 31, 32, 33, 34, 35]) {
      expect(offered).not.toContain(blocked)
    }
  })

  it("summary footer reflects all four selection facets and updates on change", () => {
    // The summary footer is the final readout the operator sees
    // before BS.9.5's Confirm picks fires. Lock that all four facets
    // surface (compile / min / preset / GMS-or-AOSP) so a copy
    // refactor can't drop one of them.
    const { rerender } = render(
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
    const summary1 = screen.getByTestId("android-api-selector-summary")
    expect(summary1.textContent).toMatch(/API 34 compile/)
    expect(summary1.textContent).toMatch(/API 26 min/)
    expect(summary1.textContent).toMatch(/Pixel Fold/)
    expect(summary1.textContent).toMatch(/GMS/)

    rerender(
      <AndroidApiSelector
        onChange={onChange}
        value={{
          compile_target: 30,
          min_api: 26,
          emulator_preset: "none",
          google_play_services: true,
        }}
      />,
    )
    const summary2 = screen.getByTestId("android-api-selector-summary")
    // 'none' preset must clear the GMS readout to "no emulator"
    // (avoid the operator misreading the summary as "emulator + GMS"
    // when nothing will be installed).
    expect(summary2.textContent).toMatch(/no emulator/)
    expect(summary2.textContent).not.toMatch(/\bGMS\b/)
  })

  it("disk total chip text matches the footer breakdown total exactly", () => {
    // The header chip and the footer total render off the same
    // ``totalBytes`` calculation. A future refactor that sourced one
    // of them from the selection (e.g., re-derived without the
    // formFactorDelta) would silently disagree. Lock byte-for-byte
    // equality + the formatted-string equality.
    render(
      <AndroidApiSelector
        onChange={onChange}
        value={{
          compile_target: 34,
          min_api: 28,
          emulator_preset: "pixel-tablet",
          google_play_services: false,
        }}
      />,
    )
    const root = screen.getByTestId("android-api-selector")
    const totalBytes = Number(root.getAttribute("data-disk-bytes"))
    const headerChip = screen.getByTestId("android-api-selector-disk-estimate")
    const footerTotal = screen.getByTestId("android-api-selector-disk-total")
    expect(Number(headerChip.getAttribute("data-bytes"))).toBe(totalBytes)
    expect(headerChip.textContent?.trim()).toBe(
      footerTotal.textContent?.trim(),
    )
    // The breakdown line bytes must sum to the same total.
    const lineSum = Array.from(
      screen.getAllByTestId(/android-api-selector-disk-line-/),
    ).reduce((acc, el) => acc + Number(el.getAttribute("data-bytes") ?? 0), 0)
    expect(lineSum).toBe(totalBytes)
  })

  it("picking pixel-tablet vs pixel-8 grows the system-image line by the form-factor delta", () => {
    // The form-factor delta on the system-image line is what makes
    // tablet / foldable picks heavier than the phone baseline. BS.9.5
    // shows this number to the operator on the AndroidApiSelector
    // sub-step — a future regression that ignored the delta would
    // mis-bill the operator's disk by ~150 MB on every tablet pick.
    const baseValue = {
      compile_target: 34 as const,
      min_api: 26 as const,
      emulator_preset: "pixel-8" as const,
      google_play_services: true,
    }
    const { rerender } = render(
      <AndroidApiSelector onChange={onChange} value={baseValue} />,
    )
    const phoneSysBytes = Number(
      screen
        .getByTestId("android-api-selector-disk-line-system-image")
        .getAttribute("data-bytes") ?? 0,
    )

    rerender(
      <AndroidApiSelector
        onChange={onChange}
        value={{ ...baseValue, emulator_preset: "pixel-tablet" }}
      />,
    )
    const tabletSysBytes = Number(
      screen
        .getByTestId("android-api-selector-disk-line-system-image")
        .getAttribute("data-bytes") ?? 0,
    )
    // Tablet system image carries the +150 MB form-factor delta.
    expect(tabletSysBytes - phoneSysBytes).toBe(150 * 1024 * 1024)
  })

  it("min API hint surfaces the current min API codename and updates on change", () => {
    // The hint copy under the Min API dropdown surfaces the codename
    // for the current min API ("Currently API 26 · Oreo.") so the
    // operator confirms the pick at a glance. Lock that the hint
    // refreshes when the parent re-renders with a new min_api.
    const { rerender } = render(
      <AndroidApiSelector
        onChange={onChange}
        value={{
          compile_target: 34,
          min_api: 26,
          emulator_preset: "pixel-8",
          google_play_services: true,
        }}
      />,
    )
    expect(
      screen.getByTestId("android-api-selector-min-api-hint").textContent,
    ).toMatch(/Oreo/)

    rerender(
      <AndroidApiSelector
        onChange={onChange}
        value={{
          compile_target: 34,
          min_api: 33,
          emulator_preset: "pixel-8",
          google_play_services: true,
        }}
      />,
    )
    expect(
      screen.getByTestId("android-api-selector-min-api-hint").textContent,
    ).toMatch(/Tiramisu/)
  })

  it("onChange does not fire on initial mount (no spurious wizard-state thrash)", () => {
    // The parent (``VerticalSetupStep`` from BS.9.5) holds
    // ``androidApi`` state and only calls ``setAndroidApi`` from this
    // callback. A spurious mount-time fire would re-render with the
    // same value, triggering an extra ``selectedNow`` reflow / loop
    // — defensible from a memoisation standpoint but a noisy waste
    // here. Lock the no-fire-on-mount contract.
    render(
      <AndroidApiSelector
        onChange={onChange}
        value={DEFAULT_ANDROID_API_SELECTION}
      />,
    )
    expect(onChange).not.toHaveBeenCalled()
  })

  it("a sequence of compile-target edits keeps min_api ≤ compile_target on every emit", () => {
    // BS.9.5's recordVerticalSetup body validates ``min_api ≤
    // compile_target`` on the backend. The component's emit path must
    // hold that invariant on every onChange so the parent never sends
    // an invalid payload. Walk a non-trivial sequence — from the
    // default (35/26) down to 28, then back up to 32, then down to
    // 24 — and assert the invariant + the snap-down behaviour.
    const value: AndroidApiSelection = {
      compile_target: 35,
      min_api: 33,
      emulator_preset: "pixel-8",
      google_play_services: true,
    }
    const { rerender } = render(
      <AndroidApiSelector onChange={onChange} value={value} />,
    )

    // Drop compile to 28 — min_api 33 must clamp to 28.
    fireEvent.change(
      screen.getByTestId("android-api-selector-compile-target"),
      { target: { value: "28" } },
    )
    const after1 = onChange.mock.calls[0][0] as AndroidApiSelection
    expect(after1.compile_target).toBe(28)
    expect(after1.min_api).toBe(28)
    expect(after1.min_api).toBeLessThanOrEqual(after1.compile_target)
    rerender(<AndroidApiSelector onChange={onChange} value={after1} />)

    // Bump compile back to 32 — min_api stays at 28 (still ≤ 32).
    fireEvent.change(
      screen.getByTestId("android-api-selector-compile-target"),
      { target: { value: "32" } },
    )
    const after2 = onChange.mock.calls[1][0] as AndroidApiSelection
    expect(after2.compile_target).toBe(32)
    expect(after2.min_api).toBe(28)
    expect(after2.min_api).toBeLessThanOrEqual(after2.compile_target)
    rerender(<AndroidApiSelector onChange={onChange} value={after2} />)

    // Drop compile to 24 — min_api 28 must clamp to 24.
    fireEvent.change(
      screen.getByTestId("android-api-selector-compile-target"),
      { target: { value: "24" } },
    )
    const after3 = onChange.mock.calls[2][0] as AndroidApiSelection
    expect(after3.compile_target).toBe(24)
    expect(after3.min_api).toBe(24)
    expect(after3.min_api).toBeLessThanOrEqual(after3.compile_target)
  })

  it("switching from 'none' to a real preset re-enables the GMS toggle and re-introduces the system-image line", () => {
    // BS.9.5's parent decides whether the operator's GMS pick matters
    // by reading ``selection.emulator_preset`` and disabling the
    // toggle when it is ``"none"``. Operator can flip the preset
    // back to a real device at any point — the toggle must come
    // alive again, the locked-hint must disappear, and the
    // system-image / emulator-runtime breakdown lines must
    // reappear in the disk footer.
    const noneValue: AndroidApiSelection = {
      compile_target: 34,
      min_api: 26,
      emulator_preset: "none",
      google_play_services: true,
    }
    const { rerender } = render(
      <AndroidApiSelector onChange={onChange} value={noneValue} />,
    )
    expect(screen.getByTestId("android-api-selector-gms-on")).toBeDisabled()
    expect(
      screen.getByTestId("android-api-selector-gms-locked-hint"),
    ).toBeInTheDocument()
    expect(
      screen.queryByTestId("android-api-selector-disk-line-system-image"),
    ).not.toBeInTheDocument()
    expect(
      screen.queryByTestId("android-api-selector-disk-line-emulator-runtime"),
    ).not.toBeInTheDocument()

    rerender(
      <AndroidApiSelector
        onChange={onChange}
        value={{ ...noneValue, emulator_preset: "pixel-8" }}
      />,
    )
    expect(screen.getByTestId("android-api-selector-gms-on")).not.toBeDisabled()
    expect(screen.getByTestId("android-api-selector-gms-off")).not.toBeDisabled()
    expect(
      screen.queryByTestId("android-api-selector-gms-locked-hint"),
    ).not.toBeInTheDocument()
    expect(
      screen.getByTestId("android-api-selector-disk-line-system-image"),
    ).toBeInTheDocument()
    expect(
      screen.getByTestId("android-api-selector-disk-line-emulator-runtime"),
    ).toBeInTheDocument()
  })
})
