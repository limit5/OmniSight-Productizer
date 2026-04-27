"use client"

/**
 * BS.9.4 — Android API range selector (sub-step inside the Mobile
 * vertical of the BS.9.3 multi-pick).
 *
 * Lives inside the per-vertical sub-step revealed when the operator
 * checks the Mobile chip in ``BootstrapVerticalStep`` (BS.9.3). This
 * form gathers everything BS.9.5's batch ``POST /installer/jobs`` needs
 * to construct an Android-SDK install job:
 *
 *   - **compile target** (``compileSdkVersion``): SDK level to compile
 *     against. Defaults to the latest stable.
 *   - **min API** (``minSdkVersion``): minimum API level the produced
 *     app supports; constrained to ``≤`` compile target.
 *   - **emulator preset**: which AVD device profile to provision (or
 *     ``none`` to skip the system-image download — saves ~2 GB).
 *   - **Google Play Services** (``google_play_services``): pull in the
 *     GMS-bundled system image vs the AOSP-only image. Same ABI either
 *     way; GMS adds ~500 MB.
 *
 * The disk estimate is computed live from the selection and shows in
 * the header chip + a per-line breakdown so the operator sees the
 * cost before committing. The numbers mirror what the BS.9.5 install
 * job will report — they're rough but operator-meaningful (within
 * ~10 % of the real ``sdkmanager --install`` footprint).
 *
 * Pure presentational + callback-driven (mirrors
 * ``BootstrapVerticalStep`` / BS.9.3):
 *
 *   - props.value: the current selection (parent owns the state so the
 *     component composes inside the BS.9.3 sub-step container; passing
 *     ``undefined`` falls back to ``DEFAULT_ANDROID_API_SELECTION``).
 *   - props.onChange(next): fires on every user edit. Parent decides
 *     when to commit (BS.9.5 batch enqueue).
 *   - props.disabled: while a parent commit is in flight (BS.9.5 will
 *     set this around the ``/installer/jobs`` call).
 *
 * Module-global state audit (per
 * ``docs/sop/implement_phase_step.md`` Step 1): zero module-level
 * mutable state. ``ANDROID_API_LEVELS`` / ``ANDROID_EMULATOR_PRESETS``
 * are frozen const tuples derived at import time. Pure helpers
 * (``coerceAndroidApiSelection`` / ``clampMinApi`` /
 * ``estimateAndroidDiskBytes`` / ``androidDiskBreakdown``) are
 * stateless. Component state is React-local and the component is
 * client-side only (no cross-worker / multi-tab sync surface).
 * Answer #1 — per-render stateless derivation.
 *
 * Read-after-write timing audit: this component does not perform any
 * write. Every change re-runs ``onChange`` synchronously with the new
 * selection so the parent renders with the up-to-date payload before
 * the next paint. BS.9.5 (the eventual batch enqueue) owns the
 * write→read race and is audited there.
 */

import { useCallback, useMemo } from "react"
import {
  Check,
  ChevronDown,
  CircleDashed,
  FoldHorizontal,
  Gauge,
  HardDrive,
  Layers,
  Smartphone,
  Tablet,
  X,
} from "lucide-react"

// ─── Domain types ───────────────────────────────────────────────────

/** Closed union of supported Android API levels — pinned so a future
 *  version-bump is a code change, not a config drift. The integer
 *  ordering doubles as compile-target ↔ min-api comparison. */
export type AndroidApiLevel =
  | 23
  | 24
  | 26
  | 28
  | 29
  | 30
  | 31
  | 32
  | 33
  | 34
  | 35

export interface AndroidApiLevelDef {
  level: AndroidApiLevel
  /** "Android 14" — used in the dropdown label. */
  versionName: string
  /** "Upside Down Cake" — secondary copy line. */
  codename: string
  /** Public release year (informational). */
  releasedYear: number
  /** Approx bytes for the SDK platform install (compile target). */
  platformSizeBytes: number
  /** Approx bytes for the GMS-bundled emulator system image at this
   *  level (matters when emulator preset != none and GMS is on). */
  systemImageGmsBytes: number
  /** Approx bytes for the AOSP-only emulator system image at this
   *  level (matters when emulator preset != none and GMS is off). */
  systemImageAospBytes: number
}

/** ``MB`` here means mebibytes (1024² bytes), matching
 *  ``formatInstallBytes`` in ``install-progress-drawer.tsx``. */
const MB = 1024 * 1024

/** Canonical Android API level catalog. Newest first so the default
 *  dropdown selection lands on the most recent stable target. Sizes
 *  are rough but operator-meaningful approximations of what
 *  ``sdkmanager --install`` actually pulls; the BS.9.5 install job
 *  will refine these once Google publishes per-package manifests. */
export const ANDROID_API_LEVELS: readonly AndroidApiLevelDef[] = [
  {
    level: 35,
    versionName: "Android 15",
    codename: "Vanilla Ice Cream",
    releasedYear: 2024,
    platformSizeBytes: 130 * MB,
    systemImageGmsBytes: 2300 * MB,
    systemImageAospBytes: 1700 * MB,
  },
  {
    level: 34,
    versionName: "Android 14",
    codename: "Upside Down Cake",
    releasedYear: 2023,
    platformSizeBytes: 125 * MB,
    systemImageGmsBytes: 2200 * MB,
    systemImageAospBytes: 1600 * MB,
  },
  {
    level: 33,
    versionName: "Android 13",
    codename: "Tiramisu",
    releasedYear: 2022,
    platformSizeBytes: 120 * MB,
    systemImageGmsBytes: 2100 * MB,
    systemImageAospBytes: 1500 * MB,
  },
  {
    level: 32,
    versionName: "Android 12L",
    codename: "Snow Cone (large)",
    releasedYear: 2022,
    platformSizeBytes: 115 * MB,
    systemImageGmsBytes: 2000 * MB,
    systemImageAospBytes: 1400 * MB,
  },
  {
    level: 31,
    versionName: "Android 12",
    codename: "Snow Cone",
    releasedYear: 2021,
    platformSizeBytes: 115 * MB,
    systemImageGmsBytes: 2000 * MB,
    systemImageAospBytes: 1400 * MB,
  },
  {
    level: 30,
    versionName: "Android 11",
    codename: "R",
    releasedYear: 2020,
    platformSizeBytes: 110 * MB,
    systemImageGmsBytes: 1900 * MB,
    systemImageAospBytes: 1300 * MB,
  },
  {
    level: 29,
    versionName: "Android 10",
    codename: "Q",
    releasedYear: 2019,
    platformSizeBytes: 105 * MB,
    systemImageGmsBytes: 1800 * MB,
    systemImageAospBytes: 1250 * MB,
  },
  {
    level: 28,
    versionName: "Android 9",
    codename: "Pie",
    releasedYear: 2018,
    platformSizeBytes: 100 * MB,
    systemImageGmsBytes: 1700 * MB,
    systemImageAospBytes: 1200 * MB,
  },
  {
    level: 26,
    versionName: "Android 8.0",
    codename: "Oreo",
    releasedYear: 2017,
    platformSizeBytes: 95 * MB,
    systemImageGmsBytes: 1500 * MB,
    systemImageAospBytes: 1100 * MB,
  },
  {
    level: 24,
    versionName: "Android 7.0",
    codename: "Nougat",
    releasedYear: 2016,
    platformSizeBytes: 90 * MB,
    systemImageGmsBytes: 1400 * MB,
    systemImageAospBytes: 1000 * MB,
  },
  {
    level: 23,
    versionName: "Android 6.0",
    codename: "Marshmallow",
    releasedYear: 2015,
    platformSizeBytes: 85 * MB,
    systemImageGmsBytes: 1300 * MB,
    systemImageAospBytes: 950 * MB,
  },
] as const

/** Canonical level list — derived from ``ANDROID_API_LEVELS`` so the
 *  source of truth stays in one place. */
export const ANDROID_API_LEVEL_VALUES: readonly AndroidApiLevel[] =
  ANDROID_API_LEVELS.map((d) => d.level)

/** Highest supported API level — used as the default ``compile_target``. */
export const ANDROID_LATEST_API_LEVEL: AndroidApiLevel = ANDROID_API_LEVEL_VALUES[0]

/** Lowest supported API level. */
export const ANDROID_OLDEST_API_LEVEL: AndroidApiLevel =
  ANDROID_API_LEVEL_VALUES[ANDROID_API_LEVEL_VALUES.length - 1]

export type AndroidEmulatorPresetId =
  | "pixel-8"
  | "pixel-6a"
  | "pixel-tablet"
  | "pixel-fold"
  | "none"

export interface AndroidEmulatorPresetDef {
  id: AndroidEmulatorPresetId
  /** Operator-facing label rendered next to the radio. */
  label: string
  /** One-line hint describing the form factor / DPI. */
  hint: string
  /** Bytes added on top of the system image (form-factor adjustments
   *  — tablet / foldable system images are slightly heavier than
   *  phone images). ``none`` means no emulator runtime + no system
   *  image, so this delta is unused. */
  formFactorDeltaBytes: number
  /** Lucide icon paired with the radio. */
  icon: React.ComponentType<{ size?: number; className?: string }>
}

export const ANDROID_EMULATOR_PRESETS: readonly AndroidEmulatorPresetDef[] = [
  {
    id: "pixel-8",
    label: "Pixel 8 (phone, hi-DPI)",
    hint: "Modern flagship phone profile, 1080×2400, 420 dpi.",
    formFactorDeltaBytes: 0,
    icon: Smartphone,
  },
  {
    id: "pixel-6a",
    label: "Pixel 6a (phone, mid-tier)",
    hint: "Mid-range phone profile, 1080×2400, 400 dpi.",
    formFactorDeltaBytes: -50 * MB,
    icon: Smartphone,
  },
  {
    id: "pixel-tablet",
    label: "Pixel Tablet (10.95″)",
    hint: "Large-screen profile for tablet UI verification.",
    formFactorDeltaBytes: 150 * MB,
    icon: Tablet,
  },
  {
    id: "pixel-fold",
    label: "Pixel Fold (foldable)",
    hint: "Inner + outer display profiles for fold-aware layouts.",
    formFactorDeltaBytes: 200 * MB,
    icon: FoldHorizontal,
  },
  {
    id: "none",
    label: "No emulator (skip system image)",
    hint: "Builds only — saves ~2 GB. Connect a physical device later.",
    formFactorDeltaBytes: 0,
    icon: CircleDashed,
  },
] as const

export const ANDROID_EMULATOR_PRESET_IDS: readonly AndroidEmulatorPresetId[] =
  ANDROID_EMULATOR_PRESETS.map((p) => p.id)

/** Always-installed pieces (regardless of choices). */
const PLATFORM_TOOLS_BYTES = 50 * MB
const BUILD_TOOLS_BYTES = 220 * MB
/** Emulator runtime binary — installed once when any preset != "none"
 *  is picked, independent of the system image. */
const EMULATOR_RUNTIME_BYTES = 600 * MB

export interface AndroidApiSelection {
  /** ``compileSdkVersion`` — locked to the supported set. */
  compile_target: AndroidApiLevel
  /** ``minSdkVersion`` — guaranteed ``≤ compile_target`` after
   *  coercion. */
  min_api: AndroidApiLevel
  /** AVD profile to provision, or "none" to skip. */
  emulator_preset: AndroidEmulatorPresetId
  /** Pull in the GMS-bundled system image (true) vs AOSP-only
   *  (false). Ignored for ``emulator_preset === "none"``. */
  google_play_services: boolean
}

export const DEFAULT_ANDROID_API_SELECTION: AndroidApiSelection = {
  compile_target: 35,
  min_api: 26,
  emulator_preset: "pixel-8",
  google_play_services: true,
}

// ─── Pure helpers (exported for unit tests + parent state coercion) ──

export function isAndroidApiLevel(value: unknown): value is AndroidApiLevel {
  return (
    typeof value === "number" &&
    (ANDROID_API_LEVEL_VALUES as readonly number[]).includes(value)
  )
}

export function isAndroidEmulatorPresetId(
  value: unknown,
): value is AndroidEmulatorPresetId {
  return (
    typeof value === "string" &&
    (ANDROID_EMULATOR_PRESET_IDS as readonly string[]).includes(value)
  )
}

/** Look up the SDK platform definition for a level. The fallback
 *  branch only exists to satisfy the type checker — ``AndroidApiLevel``
 *  is closed over ``ANDROID_API_LEVELS`` so the find always succeeds. */
export function androidApiLevelDef(level: AndroidApiLevel): AndroidApiLevelDef {
  const def = ANDROID_API_LEVELS.find((d) => d.level === level)
  return def ?? ANDROID_API_LEVELS[0]
}

export function androidEmulatorPresetDef(
  id: AndroidEmulatorPresetId,
): AndroidEmulatorPresetDef {
  const def = ANDROID_EMULATOR_PRESETS.find((p) => p.id === id)
  return def ?? ANDROID_EMULATOR_PRESETS[0]
}

/** Ensure ``min_api ≤ compile_target``; if the operator picked a
 *  newer ``min_api`` than the compile target (e.g., dropped the
 *  compile target down after picking ``min_api``), snap ``min_api``
 *  to the new compile target. */
export function clampMinApi(
  min_api: AndroidApiLevel,
  compile_target: AndroidApiLevel,
): AndroidApiLevel {
  return min_api > compile_target ? compile_target : min_api
}

/** Coerce arbitrary input into a clean ``AndroidApiSelection``.
 *  Used by the component's ``value`` prop and exported so a future
 *  re-open-step flow can rehydrate from a stored payload. */
export function coerceAndroidApiSelection(
  value: unknown,
  fallback: AndroidApiSelection = DEFAULT_ANDROID_API_SELECTION,
): AndroidApiSelection {
  if (!value || typeof value !== "object") return { ...fallback }
  const raw = value as Record<string, unknown>
  const compile_target = isAndroidApiLevel(raw.compile_target)
    ? raw.compile_target
    : fallback.compile_target
  const min_api_in = isAndroidApiLevel(raw.min_api)
    ? raw.min_api
    : fallback.min_api
  const min_api = clampMinApi(min_api_in, compile_target)
  const emulator_preset = isAndroidEmulatorPresetId(raw.emulator_preset)
    ? raw.emulator_preset
    : fallback.emulator_preset
  const google_play_services =
    typeof raw.google_play_services === "boolean"
      ? raw.google_play_services
      : fallback.google_play_services
  return { compile_target, min_api, emulator_preset, google_play_services }
}

export interface AndroidDiskBreakdownLine {
  /** Stable id for testid + key — also used as the human label. */
  id:
    | "platform-tools"
    | "compile-platform"
    | "build-tools"
    | "min-api-platform"
    | "emulator-runtime"
    | "system-image"
  label: string
  bytes: number
}

/** Per-line breakdown of the disk estimate. The component renders
 *  this as a small table; exported so tests can pin the line set
 *  without relying on DOM. */
export function androidDiskBreakdown(
  selection: AndroidApiSelection,
): AndroidDiskBreakdownLine[] {
  const compileDef = androidApiLevelDef(selection.compile_target)
  const minDef = androidApiLevelDef(selection.min_api)
  const presetDef = androidEmulatorPresetDef(selection.emulator_preset)

  const lines: AndroidDiskBreakdownLine[] = [
    {
      id: "platform-tools",
      label: "Platform tools (adb / fastboot)",
      bytes: PLATFORM_TOOLS_BYTES,
    },
    {
      id: "compile-platform",
      label: `Compile SDK platform (API ${compileDef.level})`,
      bytes: compileDef.platformSizeBytes,
    },
    {
      id: "build-tools",
      label: `Build tools ${compileDef.level}.0.0`,
      bytes: BUILD_TOOLS_BYTES,
    },
  ]

  // Min-API SDK platform — only counted when distinct from the
  // compile-target platform; otherwise the bytes are already in the
  // compile-platform line.
  if (selection.min_api !== selection.compile_target) {
    lines.push({
      id: "min-api-platform",
      label: `Min-API SDK platform (API ${minDef.level})`,
      bytes: minDef.platformSizeBytes,
    })
  }

  // Emulator + system image — skipped entirely when preset === "none".
  if (selection.emulator_preset !== "none") {
    lines.push({
      id: "emulator-runtime",
      label: "Emulator runtime",
      bytes: EMULATOR_RUNTIME_BYTES,
    })
    const sysBytes = selection.google_play_services
      ? minDef.systemImageGmsBytes
      : minDef.systemImageAospBytes
    lines.push({
      id: "system-image",
      label: `System image (API ${minDef.level}, ${
        selection.google_play_services ? "GMS" : "AOSP"
      }, ${presetDef.label.split(" (")[0]})`,
      bytes: Math.max(0, sysBytes + presetDef.formFactorDeltaBytes),
    })
  }

  return lines
}

/** Total estimated disk footprint (sum of breakdown). */
export function estimateAndroidDiskBytes(selection: AndroidApiSelection): number {
  return androidDiskBreakdown(selection).reduce((acc, line) => acc + line.bytes, 0)
}

/** Format a byte count using mebibytes / gibibytes — same unit cascade
 *  as ``formatInstallBytes`` in ``install-progress-drawer.tsx``, but
 *  re-implemented here so the component has zero coupling to BS.7's
 *  install-drawer file (which carries an SSE subscription on import). */
export function formatAndroidDiskBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B"
  const units = ["B", "KB", "MB", "GB", "TB"]
  const exp = Math.min(
    Math.floor(Math.log(bytes) / Math.log(1024)),
    units.length - 1,
  )
  const v = bytes / Math.pow(1024, exp)
  return `${v >= 100 ? v.toFixed(0) : v.toFixed(1)} ${units[exp]}`
}

// ─── Component ──────────────────────────────────────────────────────

export interface AndroidApiSelectorProps {
  /** Current selection. ``undefined`` → falls back to
   *  ``DEFAULT_ANDROID_API_SELECTION``. */
  value?: AndroidApiSelection
  /** Disable interaction (e.g., parent commit in flight). */
  disabled?: boolean
  /** Fires on every change with a coerced + clamped payload. The
   *  parent owns the state — passing a stale ``value`` back results
   *  in this callback firing again with the corrected selection on
   *  the next user edit. */
  onChange?: (next: AndroidApiSelection) => void
}

export default function AndroidApiSelector({
  value,
  disabled = false,
  onChange,
}: AndroidApiSelectorProps) {
  const selection = useMemo(
    () => coerceAndroidApiSelection(value ?? DEFAULT_ANDROID_API_SELECTION),
    [value],
  )

  const emit = useCallback(
    (patch: Partial<AndroidApiSelection>) => {
      if (disabled) return
      const merged: AndroidApiSelection = {
        ...selection,
        ...patch,
      }
      // Keep min_api ≤ compile_target whenever either side moves.
      if (
        patch.compile_target !== undefined ||
        patch.min_api !== undefined
      ) {
        merged.min_api = clampMinApi(merged.min_api, merged.compile_target)
      }
      onChange?.(merged)
    },
    [disabled, onChange, selection],
  )

  const breakdown = useMemo(() => androidDiskBreakdown(selection), [selection])
  const totalBytes = useMemo(
    () => breakdown.reduce((acc, line) => acc + line.bytes, 0),
    [breakdown],
  )

  const compileDef = androidApiLevelDef(selection.compile_target)
  const minDef = androidApiLevelDef(selection.min_api)
  const presetDef = androidEmulatorPresetDef(selection.emulator_preset)
  const gmsDisabled = disabled || selection.emulator_preset === "none"

  return (
    <div
      data-testid="android-api-selector"
      data-compile-target={selection.compile_target}
      data-min-api={selection.min_api}
      data-emulator-preset={selection.emulator_preset}
      data-gms={selection.google_play_services ? "on" : "off"}
      data-disk-bytes={totalBytes}
      data-disabled={disabled ? "true" : "false"}
      className="flex flex-col gap-3 p-3 rounded border border-[var(--border)] bg-[var(--background)]"
    >
      <div className="flex items-center gap-2 font-mono text-[10px] tracking-wider text-[var(--muted-foreground)]">
        <Smartphone size={12} />
        <span>ANDROID API SCOPE</span>
        <span
          data-testid="android-api-selector-disk-estimate"
          data-bytes={totalBytes}
          className="ml-auto inline-flex items-center gap-1 px-2 py-0.5 rounded border border-[var(--artifact-purple)]/40 bg-[var(--artifact-purple)]/10 text-[var(--foreground)]"
        >
          <HardDrive size={10} />
          <span>{formatAndroidDiskBytes(totalBytes)}</span>
        </span>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <label
          className="flex flex-col gap-1.5"
          data-testid="android-api-selector-compile-target-row"
        >
          <span className="font-mono text-[10px] tracking-wider text-[var(--muted-foreground)]">
            <Layers size={10} className="inline mr-1" />
            COMPILE TARGET
          </span>
          <span className="relative">
            <select
              data-testid="android-api-selector-compile-target"
              value={selection.compile_target}
              disabled={disabled}
              onChange={(e) =>
                emit({
                  compile_target: Number(e.target.value) as AndroidApiLevel,
                })
              }
              className="w-full appearance-none px-2 py-1.5 pr-7 rounded border border-[var(--border)] bg-[var(--background)] font-mono text-xs disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {ANDROID_API_LEVELS.map((d) => (
                <option key={d.level} value={d.level}>
                  API {d.level} — {d.versionName} ({d.codename})
                </option>
              ))}
            </select>
            <ChevronDown
              size={12}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-[var(--muted-foreground)] pointer-events-none"
            />
          </span>
          <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
            Compile against API {compileDef.level} · {compileDef.releasedYear}
          </span>
        </label>

        <label
          className="flex flex-col gap-1.5"
          data-testid="android-api-selector-min-api-row"
        >
          <span className="font-mono text-[10px] tracking-wider text-[var(--muted-foreground)]">
            <Gauge size={10} className="inline mr-1" />
            MIN API
          </span>
          <span className="relative">
            <select
              data-testid="android-api-selector-min-api"
              value={selection.min_api}
              disabled={disabled}
              onChange={(e) =>
                emit({ min_api: Number(e.target.value) as AndroidApiLevel })
              }
              className="w-full appearance-none px-2 py-1.5 pr-7 rounded border border-[var(--border)] bg-[var(--background)] font-mono text-xs disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {ANDROID_API_LEVELS.filter(
                (d) => d.level <= selection.compile_target,
              ).map((d) => (
                <option key={d.level} value={d.level}>
                  API {d.level} — {d.versionName}
                </option>
              ))}
            </select>
            <ChevronDown
              size={12}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-[var(--muted-foreground)] pointer-events-none"
            />
          </span>
          <span
            data-testid="android-api-selector-min-api-hint"
            className="font-mono text-[10px] text-[var(--muted-foreground)]"
          >
            Min ≤ compile target. Currently API {minDef.level} ·{" "}
            {minDef.codename}.
          </span>
        </label>
      </div>

      <fieldset
        data-testid="android-api-selector-emulator-presets"
        role="radiogroup"
        aria-label="Emulator preset"
        className="flex flex-col gap-1.5"
        disabled={disabled}
      >
        <legend className="font-mono text-[10px] tracking-wider text-[var(--muted-foreground)]">
          EMULATOR PRESET
        </legend>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-1.5">
          {ANDROID_EMULATOR_PRESETS.map((p) => {
            const active = selection.emulator_preset === p.id
            const Icon = p.icon
            return (
              <button
                key={p.id}
                type="button"
                role="radio"
                aria-checked={active}
                aria-label={p.label}
                data-testid={`android-api-selector-emulator-preset-${p.id}`}
                data-active={active ? "true" : "false"}
                disabled={disabled}
                onClick={() => emit({ emulator_preset: p.id })}
                className={`flex items-start gap-2 p-2 rounded border text-left transition disabled:opacity-40 disabled:cursor-not-allowed ${
                  active
                    ? "border-[var(--artifact-purple)] bg-[var(--artifact-purple)]/15"
                    : "border-[var(--border)] bg-[var(--background)] hover:border-[var(--foreground)]"
                }`}
              >
                <Icon
                  size={14}
                  className={
                    active
                      ? "mt-0.5 text-[var(--artifact-purple)]"
                      : "mt-0.5 text-[var(--muted-foreground)]"
                  }
                />
                <span className="flex flex-col gap-0.5 min-w-0">
                  <span className="font-mono text-xs font-semibold">
                    {p.label}
                  </span>
                  <span className="font-mono text-[10px] text-[var(--muted-foreground)] leading-relaxed">
                    {p.hint}
                  </span>
                </span>
                {active && (
                  <Check
                    size={12}
                    className="ml-auto shrink-0 text-[var(--artifact-purple)]"
                  />
                )}
              </button>
            )
          })}
        </div>
      </fieldset>

      <div
        data-testid="android-api-selector-gms"
        data-state={selection.google_play_services ? "on" : "off"}
        data-locked={
          selection.emulator_preset === "none" ? "true" : "false"
        }
        className="flex flex-col gap-1.5"
      >
        <span className="font-mono text-[10px] tracking-wider text-[var(--muted-foreground)]">
          GOOGLE PLAY SERVICES
        </span>
        <div
          role="radiogroup"
          aria-label="Google Play Services"
          className="inline-flex rounded border border-[var(--border)] overflow-hidden font-mono text-xs"
        >
          <button
            type="button"
            role="radio"
            aria-checked={selection.google_play_services}
            data-testid="android-api-selector-gms-on"
            data-active={selection.google_play_services ? "true" : "false"}
            disabled={gmsDisabled}
            onClick={() => emit({ google_play_services: true })}
            className={`flex items-center gap-1.5 px-3 py-1.5 transition disabled:opacity-40 disabled:cursor-not-allowed ${
              selection.google_play_services
                ? "bg-[var(--artifact-purple)] text-white"
                : "bg-[var(--background)] text-[var(--foreground)] hover:bg-[var(--muted)]/40"
            }`}
          >
            <Check size={11} />
            With GMS (Play Store APIs)
          </button>
          <button
            type="button"
            role="radio"
            aria-checked={!selection.google_play_services}
            data-testid="android-api-selector-gms-off"
            data-active={!selection.google_play_services ? "true" : "false"}
            disabled={gmsDisabled}
            onClick={() => emit({ google_play_services: false })}
            className={`flex items-center gap-1.5 px-3 py-1.5 transition disabled:opacity-40 disabled:cursor-not-allowed border-l border-[var(--border)] ${
              !selection.google_play_services
                ? "bg-[var(--artifact-purple)] text-white"
                : "bg-[var(--background)] text-[var(--foreground)] hover:bg-[var(--muted)]/40"
            }`}
          >
            <X size={11} />
            AOSP only (no GMS)
          </button>
        </div>
        {selection.emulator_preset === "none" && (
          <span
            data-testid="android-api-selector-gms-locked-hint"
            className="font-mono text-[10px] text-[var(--muted-foreground)]"
          >
            No emulator selected — GMS toggle has no effect (no system
            image will be installed).
          </span>
        )}
      </div>

      <div
        data-testid="android-api-selector-disk-breakdown"
        className="flex flex-col gap-1 p-2 rounded border border-dashed border-[var(--border)] bg-[var(--muted)]/20"
      >
        <div className="flex items-center gap-2 font-mono text-[10px] tracking-wider text-[var(--muted-foreground)]">
          <HardDrive size={11} />
          <span>DISK ESTIMATE</span>
          <span
            data-testid="android-api-selector-disk-total"
            className="ml-auto font-mono text-[11px] text-[var(--foreground)]"
          >
            {formatAndroidDiskBytes(totalBytes)}
          </span>
        </div>
        <ul className="flex flex-col gap-0.5 list-none p-0 m-0">
          {breakdown.map((line) => (
            <li
              key={line.id}
              data-testid={`android-api-selector-disk-line-${line.id}`}
              data-bytes={line.bytes}
              className="flex items-center gap-2 font-mono text-[10px] text-[var(--muted-foreground)]"
            >
              <span className="truncate">{line.label}</span>
              <span className="ml-auto text-[var(--foreground)]">
                {formatAndroidDiskBytes(line.bytes)}
              </span>
            </li>
          ))}
        </ul>
        <span
          data-testid="android-api-selector-summary"
          className="font-mono text-[10px] text-[var(--muted-foreground)] mt-1 leading-relaxed"
        >
          API {compileDef.level} compile · API {minDef.level} min ·{" "}
          {presetDef.label.split(" (")[0]} ·{" "}
          {selection.emulator_preset === "none"
            ? "no emulator"
            : selection.google_play_services
              ? "GMS"
              : "AOSP"}
        </span>
      </div>
    </div>
  )
}
