/**
 * V6 #3 (TODO row 1550 / issue #322) — Device frame renderer.
 *
 * Wraps a mobile emulator/simulator screenshot in a hardware-accurate
 * device bezel so the operator sees *exactly* what the user would see,
 * not just a raw 1179×2556 PNG. Six presets covering the mobile-grid
 * scope that V6 #4 `device-grid.tsx` will fan out to:
 *
 *   - `iphone-15`   — iPhone 15 / 15 Pro (Dynamic Island, 393×852 pt)
 *   - `iphone-se`   — iPhone SE 3rd gen  (physical home button, 375×667 pt)
 *   - `ipad`        — iPad 10th gen       (820×1180 pt, bezelled tablet)
 *   - `pixel-8`     — Pixel 8             (camera hole-punch, 412×915 dp)
 *   - `galaxy-fold` — Galaxy Z Fold 5     (narrow outer cover display)
 *   - `galaxy-tab`  — Galaxy Tab S9       (Android tablet, 800×1280 dp)
 *
 * Why pure CSS/SVG (no external PNG bezel assets):
 *   - Ships with the bundle — no extra network round-trip on first paint
 *     of a grid of six devices (V6 #4).
 *   - Deterministic in jsdom/vitest and the visual-regression pipeline.
 *   - Independent of the designer's Figma asset library — the frame is
 *     code, not an export.
 *
 * Geometry:
 *   Every profile carries its *native pixel* screen size plus bezel
 *   thicknesses and corner radii. The renderer computes a scale factor
 *   from the caller's `width` prop (CSS px) down to the profile's
 *   native width, then scales every dimension uniformly — so a Pixel 8
 *   at `width=200` stays proportional to its native 1080×2400.
 *
 * Screenshot fit:
 *   The underlying PNG is `object-fit: cover` inside the screen cut-out,
 *   never `contain`, because the screenshot is already produced at the
 *   device's native resolution (V6 #1/#2). Under-sized screenshots (a
 *   caller feeding a wrong-aspect thumbnail) crop to the visible screen
 *   region rather than distort. Missing / loading / empty states fall
 *   back to a muted placeholder.
 *
 * Downstream consumers:
 *   - V6 #4 `device-grid.tsx` — renders 6+ frames in a responsive grid.
 *   - V6 #5 agent visual context — uses `DEVICE_PROFILES` dimensions to
 *     tell the multimodal LLM "this is what an iPhone 15 user sees".
 *   - V7 mobile visual annotator — overlays annotation rects inside the
 *     same screen cut-out so hit-testing matches what the user clicked.
 */
"use client"

import * as React from "react"
import { cn } from "@/lib/utils"

// ─── Public shapes ─────────────────────────────────────────────────────────

export type DevicePlatform = "ios" | "android"

export type DeviceForm = "phone" | "tablet" | "foldable"

export type DeviceCutoutKind = "island" | "notch" | "hole" | "bar" | "none"

export type DeviceProfileId =
  | "iphone-15"
  | "iphone-se"
  | "ipad"
  | "pixel-8"
  | "galaxy-fold"
  | "galaxy-tab"

export interface DeviceCutout {
  /** Shape of the front-facing camera housing. */
  kind: DeviceCutoutKind
  /** Native-pixel width of the cutout. */
  width: number
  /** Native-pixel height of the cutout. */
  height: number
  /** Distance from the top edge of the *screen* (not the frame). */
  offsetY: number
}

export interface DeviceProfile {
  id: DeviceProfileId
  label: string
  platform: DevicePlatform
  form: DeviceForm
  /** Portrait-orientation *native pixel* width of the active screen. */
  screenWidth: number
  /** Portrait-orientation *native pixel* height of the active screen. */
  screenHeight: number
  /** Bezel thickness in native pixels — sides may differ (home button, chin). */
  bezel: { top: number; right: number; bottom: number; left: number }
  /** Outer-frame rounding in native pixels. */
  frameRadius: number
  /** Inner screen rounding in native pixels (typically smaller than frame). */
  screenRadius: number
  /** Camera / notch / island description. `none` = old iPad/iPhone-SE bar. */
  cutout: DeviceCutout
  /** SE/iPad have a physical home button rendered in the chin. */
  homeButton: boolean
  /** CSS color for the frame bezel fill. */
  frameColor: string
  /** CSS color for the frame edge stroke. */
  frameStroke: string
}

export interface DeviceFrameProps {
  /** Which preset to render. */
  device: DeviceProfileId
  /** Screenshot to place inside the screen cut-out. */
  screenshotUrl?: string | null
  /** Alternate text for the screenshot — falls back to device label. */
  alt?: string
  /** Desired rendered width in CSS px — scales the entire frame. Default 280. */
  width?: number
  /** Extra classes on the outer `<figure>`. */
  className?: string
  /** Show the device name label below the frame. Default `false`. */
  showLabel?: boolean
  /** Shimmer placeholder instead of the screenshot. */
  loading?: boolean
  /** Empty-state placeholder ("no screenshot yet"). */
  empty?: boolean
  /** Click handler — used by the grid to select a device. */
  onClick?: () => void
  /** `data-testid` passthrough for tests & device-grid selection. */
  "data-testid"?: string
}

// ─── Profile catalogue ─────────────────────────────────────────────────────

/**
 * All measurements are in *native device pixels* — the same units the
 * emulator/simulator screenshots come back in (V6 #1/#2 parse IHDR and
 * check `1179×2556` etc.). Scaling to CSS pixels happens at render time.
 *
 * Bezel / corner figures are tuned for visual plausibility rather than
 * engineering-drawing accuracy: the goal is "the operator recognises
 * this as an iPhone vs a Pixel at a glance", not a teardown.
 */
export const DEVICE_PROFILES: Readonly<Record<DeviceProfileId, DeviceProfile>> = Object.freeze({
  "iphone-15": {
    id: "iphone-15",
    label: "iPhone 15",
    platform: "ios",
    form: "phone",
    screenWidth: 1179,
    screenHeight: 2556,
    bezel: { top: 60, right: 60, bottom: 60, left: 60 },
    frameRadius: 180,
    screenRadius: 140,
    cutout: { kind: "island", width: 380, height: 110, offsetY: 40 },
    homeButton: false,
    frameColor: "#111418",
    frameStroke: "#2a2f36",
  },
  "iphone-se": {
    id: "iphone-se",
    label: "iPhone SE",
    platform: "ios",
    form: "phone",
    screenWidth: 750,
    screenHeight: 1334,
    bezel: { top: 170, right: 36, bottom: 210, left: 36 },
    frameRadius: 80,
    screenRadius: 0,
    cutout: { kind: "bar", width: 220, height: 14, offsetY: -90 },
    homeButton: true,
    frameColor: "#d6d8dc",
    frameStroke: "#9aa0a6",
  },
  ipad: {
    id: "ipad",
    label: "iPad",
    platform: "ios",
    form: "tablet",
    screenWidth: 1640,
    screenHeight: 2360,
    bezel: { top: 120, right: 120, bottom: 120, left: 120 },
    frameRadius: 120,
    screenRadius: 20,
    cutout: { kind: "hole", width: 24, height: 24, offsetY: -60 },
    homeButton: false,
    frameColor: "#e4e6ea",
    frameStroke: "#a5aab1",
  },
  "pixel-8": {
    id: "pixel-8",
    label: "Pixel 8",
    platform: "android",
    form: "phone",
    screenWidth: 1080,
    screenHeight: 2400,
    bezel: { top: 56, right: 40, bottom: 56, left: 40 },
    frameRadius: 160,
    screenRadius: 120,
    cutout: { kind: "hole", width: 90, height: 90, offsetY: 40 },
    homeButton: false,
    frameColor: "#1b1e22",
    frameStroke: "#3a3f46",
  },
  "galaxy-fold": {
    id: "galaxy-fold",
    label: "Galaxy Fold",
    platform: "android",
    form: "foldable",
    screenWidth: 904,
    screenHeight: 2176,
    bezel: { top: 80, right: 48, bottom: 80, left: 48 },
    frameRadius: 80,
    screenRadius: 40,
    cutout: { kind: "hole", width: 80, height: 80, offsetY: 50 },
    homeButton: false,
    frameColor: "#14181d",
    frameStroke: "#353a42",
  },
  "galaxy-tab": {
    id: "galaxy-tab",
    label: "Galaxy Tab",
    platform: "android",
    form: "tablet",
    screenWidth: 1600,
    screenHeight: 2560,
    bezel: { top: 110, right: 110, bottom: 110, left: 110 },
    frameRadius: 90,
    screenRadius: 16,
    cutout: { kind: "hole", width: 28, height: 28, offsetY: -70 },
    homeButton: false,
    frameColor: "#22262d",
    frameStroke: "#444a54",
  },
})

export const DEVICE_PROFILE_IDS: readonly DeviceProfileId[] = Object.freeze(
  Object.keys(DEVICE_PROFILES) as DeviceProfileId[],
)

export function getDeviceProfile(id: DeviceProfileId): DeviceProfile {
  const profile = DEVICE_PROFILES[id]
  if (!profile) {
    // Defensive — the type system should already prevent this, but a
    // stray string at runtime (feature flag, bad URL param) shouldn't
    // crash the whole grid.
    throw new Error(`Unknown device profile: ${id}`)
  }
  return profile
}

// ─── Geometry helpers ──────────────────────────────────────────────────────

/**
 * Full native-pixel outer dimensions including bezels. Used by the
 * renderer and by callers that want to lay out a grid *without*
 * instantiating every DOM node first (V6 #4).
 */
export function getDeviceOuterSize(profile: DeviceProfile): { width: number; height: number } {
  return {
    width: profile.screenWidth + profile.bezel.left + profile.bezel.right,
    height: profile.screenHeight + profile.bezel.top + profile.bezel.bottom,
  }
}

/**
 * Compute the CSS-px scale for a caller-supplied render width. The
 * scale is uniform — native-pixel geometry stays proportional.
 */
export function computeDeviceScale(profile: DeviceProfile, renderWidth: number): number {
  const outer = getDeviceOuterSize(profile)
  if (!Number.isFinite(renderWidth) || renderWidth <= 0) return 0
  return renderWidth / outer.width
}

// ─── Component ─────────────────────────────────────────────────────────────

const DEFAULT_WIDTH = 280
const MIN_WIDTH = 48

export function DeviceFrame({
  device,
  screenshotUrl = null,
  alt,
  width = DEFAULT_WIDTH,
  className,
  showLabel = false,
  loading = false,
  empty = false,
  onClick,
  "data-testid": testId,
}: DeviceFrameProps) {
  const profile = getDeviceProfile(device)
  const renderWidth = Math.max(MIN_WIDTH, Number.isFinite(width) ? width : DEFAULT_WIDTH)
  const scale = computeDeviceScale(profile, renderWidth)
  const outer = getDeviceOuterSize(profile)
  const renderHeight = outer.height * scale

  // ── Scaled geometry (all in CSS px from here down) ────────────────
  const frameRadius = profile.frameRadius * scale
  const screenRadius = profile.screenRadius * scale
  const bezelTop = profile.bezel.top * scale
  const bezelLeft = profile.bezel.left * scale
  const screenWidthCss = profile.screenWidth * scale
  const screenHeightCss = profile.screenHeight * scale

  // Dynamic Island / notch / hole-punch: positioned relative to the
  // screen top edge so the cutout lands on the status-bar row even as
  // different profiles move the hardware around.
  const cutoutRenderable =
    profile.cutout.kind !== "none" &&
    profile.cutout.width > 0 &&
    profile.cutout.height > 0

  const cutoutWidthCss = profile.cutout.width * scale
  const cutoutHeightCss = profile.cutout.height * scale
  const cutoutLeftCss = bezelLeft + (screenWidthCss - cutoutWidthCss) / 2
  const cutoutTopCss = bezelTop + profile.cutout.offsetY * scale

  // Home button (SE / pre-X iPads) — rendered inside the chin, centred.
  const homeButtonSizeCss = profile.homeButton
    ? Math.min(profile.bezel.bottom * 0.55, profile.screenWidth * 0.1) * scale
    : 0

  const effectiveAlt = alt ?? `${profile.label} screenshot`
  const showScreenshot = !loading && !empty && !!screenshotUrl

  const figureStyle: React.CSSProperties = {
    width: renderWidth,
    // Locks height so the frame never collapses in a flex grid before
    // the screenshot has loaded.
    height: renderHeight + (showLabel ? 22 : 0),
  }

  const frameStyle: React.CSSProperties = {
    width: renderWidth,
    height: renderHeight,
    borderRadius: frameRadius,
    background: profile.frameColor,
    border: `1px solid ${profile.frameStroke}`,
    // A soft inner ring sells the chamfered edge of a real device
    // without a second asset. Matches the 1px stroke on the outside.
    boxShadow: `inset 0 0 0 1px rgba(255,255,255,0.05), 0 6px 20px rgba(0,0,0,0.25)`,
  }

  const screenStyle: React.CSSProperties = {
    position: "absolute",
    top: bezelTop,
    left: bezelLeft,
    width: screenWidthCss,
    height: screenHeightCss,
    borderRadius: screenRadius,
    // Screenshots come back premultiplied against black (adb screencap,
    // simctl io); matching the placeholder avoids a white flash.
    background: "#000",
    overflow: "hidden",
  }

  const cutoutRadius =
    profile.cutout.kind === "hole"
      ? Math.min(cutoutWidthCss, cutoutHeightCss) / 2
      : profile.cutout.kind === "island" || profile.cutout.kind === "notch"
      ? cutoutHeightCss / 2
      : 2

  const cutoutStyle: React.CSSProperties = {
    position: "absolute",
    top: cutoutTopCss,
    left: cutoutLeftCss,
    width: cutoutWidthCss,
    height: cutoutHeightCss,
    borderRadius: cutoutRadius,
    background: "#000",
    border: "1px solid rgba(255,255,255,0.04)",
    // Rendered above the screenshot — the camera housing occludes the
    // status-bar pixels in real hardware too.
    zIndex: 2,
  }

  const homeButtonStyle: React.CSSProperties = profile.homeButton
    ? {
        position: "absolute",
        bottom: (profile.bezel.bottom * scale - homeButtonSizeCss) / 2,
        left: (renderWidth - homeButtonSizeCss) / 2,
        width: homeButtonSizeCss,
        height: homeButtonSizeCss,
        borderRadius: homeButtonSizeCss / 2,
        background: "rgba(0,0,0,0.08)",
        border: `1px solid ${profile.frameStroke}`,
      }
    : {}

  return (
    <figure
      className={cn(
        "omnisight-device-frame relative inline-flex flex-col items-center select-none",
        onClick && "cursor-pointer",
        className,
      )}
      style={figureStyle}
      data-testid={testId}
      data-device={profile.id}
      data-platform={profile.platform}
      data-form={profile.form}
      role={onClick ? "button" : undefined}
      tabIndex={onClick ? 0 : undefined}
      onClick={onClick}
      onKeyDown={
        onClick
          ? (e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault()
                onClick()
              }
            }
          : undefined
      }
      aria-label={`${profile.label} device frame`}
    >
      <div
        className="omnisight-device-frame__bezel relative"
        style={frameStyle}
        data-testid={testId ? `${testId}-bezel` : undefined}
      >
        <div
          className="omnisight-device-frame__screen"
          style={screenStyle}
          data-testid={testId ? `${testId}-screen` : undefined}
        >
          {showScreenshot && (
            // eslint-disable-next-line @next/next/no-img-element -- screenshots
            // are runtime-user content (emulator capture) with unknown
            // remote hosts, so Next/Image's static domain allowlist and
            // optimisation pipeline doesn't apply here.
            <img
              src={screenshotUrl!}
              alt={effectiveAlt}
              draggable={false}
              style={{
                width: "100%",
                height: "100%",
                objectFit: "cover",
                objectPosition: "top center",
                display: "block",
              }}
              data-testid={testId ? `${testId}-screenshot` : undefined}
            />
          )}
          {loading && (
            <div
              className="omnisight-device-frame__loading"
              role="status"
              aria-label="loading screenshot"
              style={{
                width: "100%",
                height: "100%",
                background:
                  "linear-gradient(110deg, rgba(255,255,255,0.04) 0%, rgba(255,255,255,0.12) 50%, rgba(255,255,255,0.04) 100%)",
                backgroundSize: "200% 100%",
                animation: "omnisight-device-frame-shimmer 1.4s linear infinite",
              }}
            />
          )}
          {empty && !loading && (
            <div
              className="omnisight-device-frame__empty flex items-center justify-center text-[10px] font-mono"
              style={{ width: "100%", height: "100%", color: "rgba(255,255,255,0.4)" }}
            >
              no screenshot
            </div>
          )}
        </div>
        {cutoutRenderable && (
          <div
            className="omnisight-device-frame__cutout"
            style={cutoutStyle}
            data-cutout={profile.cutout.kind}
            aria-hidden
          />
        )}
        {profile.homeButton && (
          <div
            className="omnisight-device-frame__home"
            style={homeButtonStyle}
            aria-hidden
            data-testid={testId ? `${testId}-home` : undefined}
          />
        )}
      </div>
      {showLabel && (
        <figcaption
          className="omnisight-device-frame__label mt-1 text-[10px] font-mono uppercase tracking-wider text-[var(--muted-foreground,#94a3b8)]"
          data-testid={testId ? `${testId}-label` : undefined}
        >
          {profile.label}
        </figcaption>
      )}
      {/* Shimmer keyframes inlined so the component is self-contained —
          V6 #4 device-grid mounts six of these without needing a global
          style import. */}
      <style>{`
        @keyframes omnisight-device-frame-shimmer {
          0%   { background-position: 200% 0; }
          100% { background-position: -200% 0; }
        }
      `}</style>
    </figure>
  )
}

export default DeviceFrame
