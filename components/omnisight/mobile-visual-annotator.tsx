/**
 * V7 row 1732 (#323 first bullet) — Mobile visual annotation.
 *
 * Wraps a `DeviceFrame` (V6 #3, `components/omnisight/device-frame.tsx`)
 * in a draw/click annotation overlay pinned to the *screen cut-out* —
 * not the outer bezel — so the operator can circle or pin any pixel
 * the simulator/emulator actually renders and the agent gets a
 * normalised coordinate that lines up with the native screen buffer.
 *
 * Unlike V3 #1 `visual-annotator.tsx` which operates on a flat Web
 * screenshot, the Mobile annotator carries two extra hints the agent
 * must know to patch the right source file:
 *
 *   - `platform`: `"swiftui" | "compose" | "flutter" | "react-native"`
 *     — the active toolchain for this workspace.  Determined by the
 *     sidebar platform selector (see
 *     `DEFAULT_MOBILE_PLATFORMS` in `workspace-navigation-sidebar.tsx`)
 *     and echoed into every annotation payload so the agent skill
 *     can route to the correct file (SwiftUI `*.swift`, Compose
 *     `*.kt`, Flutter `*.dart`, RN `*.tsx`).
 *   - `device`: the `DeviceProfileId` of the captured screenshot so
 *     the agent knows native pixel → dp/pt scaling and which cutout
 *     position eats into the status-bar region.
 *
 * The overlay coordinates remain normalised to the *screen region*
 * (the inner cut-out, not the outer bezel rect).  This matches what
 * iOS `XCUIElement.frame` and Android `UiObject2.getVisibleBounds`
 * return — so an agent that wants to hit-test back from an
 * annotation's `boundingBox` against a UI hierarchy dump does so in
 * the native coordinate space, not CSS-bezel space.
 *
 * Gesture + selection semantics mirror V3 #1 exactly:
 *   - `rect` mode: drag to draw a bounding rectangle.
 *   - `click` mode: single click places a zero-size pin.
 *   - `select` mode: click an existing annotation to focus it.
 *   - Keyboard `Delete` / `Backspace` removes the selected annotation.
 *   - Drafts below `rectMinNormalized` demote to click points.
 *
 * Downstream consumers:
 *   - V7 row 1734-1736 Mobile workspace page (`app/workspace/mobile/
 *     page.tsx`) will mount this inside the center pane's device
 *     frame grid.
 *   - `backend/mobile_annotation_context.py` (V7 server twin, landed
 *     alongside this file) consumes the payload `toMobileAgentPayload`
 *     produces and renders a Swift / Kotlin / Dart / TSX-aware
 *     markdown block for the mobile agent's next ReAct turn.
 */
"use client"

import * as React from "react"
import { MousePointer2, Square, Target, Trash2, X } from "lucide-react"

import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"

import {
  DeviceFrame,
  getDeviceProfile,
  type DeviceProfile,
  type DeviceProfileId,
} from "@/components/omnisight/device-frame"
import {
  clampNormalized,
  hitTestNormalizedBox,
  pointsToNormalizedBox,
  type NormalizedBoundingBox,
  type OverlayRect,
  type VisualAnnotation,
  type VisualAnnotatorMode,
  type VisualAnnotationType,
} from "@/components/omnisight/visual-annotator"

// ─── Public shapes ─────────────────────────────────────────────────────────

/**
 * Toolchain for the workspace.  Matches the `id` field of
 * `DEFAULT_MOBILE_PLATFORMS` in `workspace-navigation-sidebar.tsx`:
 * `"ios" | "android" | "flutter" | "react-native"`.  The agent side
 * maps `ios → swiftui`, `android → compose` (Jetpack), and uses
 * `flutter` / `react-native` as-is — this union is the *workspace*
 * vocabulary, not the registry's.
 */
export type MobilePlatform = "ios" | "android" | "flutter" | "react-native"

/**
 * The four framework identifiers the agent-side skills key off.  We
 * keep this distinct from `MobilePlatform` so iOS + SwiftUI parity
 * (iOS may one day cover UIKit fallbacks) can evolve independently.
 */
export type MobileFramework =
  | "swiftui"
  | "jetpack-compose"
  | "flutter"
  | "react-native"

export const MOBILE_PLATFORM_TO_FRAMEWORK: Readonly<
  Record<MobilePlatform, MobileFramework>
> = Object.freeze({
  ios: "swiftui",
  android: "jetpack-compose",
  flutter: "flutter",
  "react-native": "react-native",
})

/** File-extension fingerprint the agent uses to search its workspace. */
export const FRAMEWORK_TO_FILE_EXT: Readonly<Record<MobileFramework, string>> =
  Object.freeze({
    swiftui: ".swift",
    "jetpack-compose": ".kt",
    flutter: ".dart",
    "react-native": ".tsx",
  })

export interface MobileVisualAnnotation extends VisualAnnotation {
  /**
   * Platform-specific selector resolved by a future element inspector
   * (iOS accessibility identifier, Compose test-tag, Flutter Key,
   * RN testID).  The overlay itself never fabricates one — callers
   * who have an inspector wired may inject a value per-annotation.
   */
  componentHint?: string | null
}

export interface MobileVisualAnnotationAgentPayload {
  /** Matches `VisualAnnotationAgentPayload.type` — `click` | `rect`. */
  type: VisualAnnotationType
  /** Platform (workspace vocabulary — `ios` / `android` / ...). */
  platform: MobilePlatform
  /** Target framework (`swiftui` / `jetpack-compose` / ...). */
  framework: MobileFramework
  /** File-ext hint (`.swift`, `.kt`, `.dart`, `.tsx`). */
  fileExt: string
  /** Device profile id so the agent knows native pixel geometry. */
  device: DeviceProfileId
  /** Native-pixel screen size of the selected device (portrait). */
  screenWidth: number
  /** Native-pixel screen height of the selected device (portrait). */
  screenHeight: number
  /** Normalised `[0, 1]` box *inside the screen cut-out*. */
  boundingBox: NormalizedBoundingBox
  /** Same box expressed in native device pixels — convenience for the agent. */
  nativePixelBox: { x: number; y: number; w: number; h: number }
  /** Optional native-side element identifier (Compose test-tag, RN testID, etc.). */
  componentHint: string | null
  /** Operator comment — may be empty, never undefined. */
  comment: string
}

export interface MobileVisualAnnotatorProps {
  /** Screenshot URL to place inside the device frame.  Missing → empty state. */
  screenshotUrl?: string | null
  /** Screenshot alt text — defaults to a neutral preview label. */
  screenshotAlt?: string
  /** Which device profile to render.  Default `"iphone-15"`. */
  device?: DeviceProfileId
  /** Active workspace platform.  Default `"ios"`. */
  platform?: MobilePlatform
  /** Controlled annotation list. */
  annotations?: MobileVisualAnnotation[]
  /** Uncontrolled initial annotations. */
  defaultAnnotations?: MobileVisualAnnotation[]
  /** Fired after every annotation mutation. */
  onAnnotationsChange?: (next: MobileVisualAnnotation[]) => void
  /** Controlled selection id. */
  selectedId?: string | null
  /** Uncontrolled initial selection. */
  defaultSelectedId?: string | null
  /** Fired on selection change. */
  onSelectionChange?: (id: string | null) => void
  /** Controlled active mode (`rect` / `click` / `select`). */
  mode?: VisualAnnotatorMode
  /** Uncontrolled initial mode — defaults to `"rect"`. */
  defaultMode?: VisualAnnotatorMode
  /** Fired on mode change. */
  onModeChange?: (mode: VisualAnnotatorMode) => void
  /** Minimum normalised size before a rect demotes to a click. */
  rectMinNormalized?: number
  /** Rendered device-frame width in CSS px (outer, including bezel).  Default 280. */
  frameWidth?: number
  /** Disable all mutation surfaces. */
  disabled?: boolean
  /** Factory for new annotation ids — swap out in tests for determinism. */
  idFactory?: () => string
  /** Clock seam for `createdAt` / `updatedAt`. */
  nowIso?: () => string
  /** Test-only seam — returns the overlay's client-space rect. */
  getOverlayRect?: () => OverlayRect
  /**
   * Fired when the operator clicks "Send to agent".  Receives the
   * structured payload list already keyed to the active platform +
   * device; caller is responsible for POSTing to
   * `POST /api/v1/mobile-annotation-context/build` (see
   * `backend/mobile_annotation_context.py`).
   */
  onSendToAgent?: (
    payloads: MobileVisualAnnotationAgentPayload[],
  ) => void | Promise<void>
  /** Optional className on the outer `<section>`. */
  className?: string
  /** Test-id passthrough. */
  "data-testid"?: string
}

// ─── Defaults / constants ─────────────────────────────────────────────────

const DEFAULT_RECT_MIN_NORMALIZED = 0.01
const DEFAULT_FRAME_WIDTH = 280
const DEFAULT_DEVICE: DeviceProfileId = "iphone-15"
const DEFAULT_PLATFORM: MobilePlatform = "ios"

const MODE_LABEL: Record<VisualAnnotatorMode, string> = {
  rect: "Draw rectangle",
  click: "Pin click point",
  select: "Select annotation",
}

// ─── Pure helpers (exported for unit tests) ───────────────────────────────

/**
 * ID factory mirroring the frontend visual-annotator default.  Kept
 * local so mobile callers don't need to reach into V3 #1 internals.
 */
export function defaultMobileAnnotatorIdFactory(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID()
  }
  return `mann-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

export function defaultMobileAnnotatorNowIso(): string {
  return new Date().toISOString()
}

/** Resolve the target framework for a workspace platform. */
export function resolveFramework(platform: MobilePlatform): MobileFramework {
  return MOBILE_PLATFORM_TO_FRAMEWORK[platform]
}

/** Resolve the canonical file-extension hint for a framework. */
export function resolveFileExt(framework: MobileFramework): string {
  return FRAMEWORK_TO_FILE_EXT[framework]
}

/**
 * Compute native-pixel geometry of a normalised screen-region box for
 * the supplied device profile.  `w` and `h` clamp against the bottom-
 * right corner so a poorly-specified bounding box never escapes the
 * screen rectangle — we'd rather crop than emit a >screenHeight value
 * the agent then tries to interpret as a status-bar overlay.
 */
export function normalizedToNativePixels(
  box: NormalizedBoundingBox,
  profile: DeviceProfile,
): { x: number; y: number; w: number; h: number } {
  const clampedX = clampNormalized(box.x)
  const clampedY = clampNormalized(box.y)
  const clampedW = clampNormalized(box.w)
  const clampedH = clampNormalized(box.h)
  const x = Math.round(clampedX * profile.screenWidth)
  const y = Math.round(clampedY * profile.screenHeight)
  const maxW = Math.max(0, profile.screenWidth - x)
  const maxH = Math.max(0, profile.screenHeight - y)
  const w = Math.min(Math.round(clampedW * profile.screenWidth), maxW)
  const h = Math.min(Math.round(clampedH * profile.screenHeight), maxH)
  return { x, y, w, h }
}

/**
 * Flatten a mobile annotation into the wire shape the backend
 * `MobileVisualAnnotationAgentPayload` pins.  Pure — no clock, no
 * DOM access — so unit tests and callers outside the React tree can
 * drive it directly.
 */
export function toMobileAgentPayload(
  annotation: MobileVisualAnnotation,
  platform: MobilePlatform,
  device: DeviceProfileId,
): MobileVisualAnnotationAgentPayload {
  const profile = getDeviceProfile(device)
  const framework = resolveFramework(platform)
  const nativePixelBox = normalizedToNativePixels(annotation.boundingBox, profile)
  return {
    type: annotation.type,
    platform,
    framework,
    fileExt: resolveFileExt(framework),
    device,
    screenWidth: profile.screenWidth,
    screenHeight: profile.screenHeight,
    boundingBox: {
      x: clampNormalized(annotation.boundingBox.x),
      y: clampNormalized(annotation.boundingBox.y),
      w: clampNormalized(annotation.boundingBox.w),
      h: clampNormalized(annotation.boundingBox.h),
    },
    nativePixelBox,
    componentHint: annotation.componentHint ?? null,
    comment: annotation.comment ?? "",
  }
}

export function toMobileAgentPayloads(
  annotations: MobileVisualAnnotation[],
  platform: MobilePlatform,
  device: DeviceProfileId,
): MobileVisualAnnotationAgentPayload[] {
  return annotations.map((a) => toMobileAgentPayload(a, platform, device))
}

// ─── Component ─────────────────────────────────────────────────────────────

interface PendingDraft {
  startX: number
  startY: number
  currentX: number
  currentY: number
  pointerId: number
}

export function MobileVisualAnnotator({
  screenshotUrl = null,
  screenshotAlt,
  device = DEFAULT_DEVICE,
  platform = DEFAULT_PLATFORM,
  annotations,
  defaultAnnotations,
  onAnnotationsChange,
  selectedId,
  defaultSelectedId,
  onSelectionChange,
  mode,
  defaultMode = "rect",
  onModeChange,
  rectMinNormalized = DEFAULT_RECT_MIN_NORMALIZED,
  frameWidth = DEFAULT_FRAME_WIDTH,
  disabled = false,
  idFactory = defaultMobileAnnotatorIdFactory,
  nowIso = defaultMobileAnnotatorNowIso,
  getOverlayRect,
  onSendToAgent,
  className,
  "data-testid": testId,
}: MobileVisualAnnotatorProps) {
  const profile = React.useMemo(() => getDeviceProfile(device), [device])
  const framework = React.useMemo(() => resolveFramework(platform), [platform])
  const fileExt = React.useMemo(() => resolveFileExt(framework), [framework])

  // ─ Controlled / uncontrolled state wiring ─────────────────────────────
  const isAnnotationsControlled = annotations !== undefined
  const [internalAnnotations, setInternalAnnotations] = React.useState<
    MobileVisualAnnotation[]
  >(() => (defaultAnnotations ? defaultAnnotations.map((a) => ({ ...a })) : []))
  const effectiveAnnotations = isAnnotationsControlled
    ? (annotations as MobileVisualAnnotation[])
    : internalAnnotations

  const isSelectionControlled = selectedId !== undefined
  const [internalSelectedId, setInternalSelectedId] = React.useState<string | null>(
    defaultSelectedId ?? null,
  )
  const effectiveSelectedId = isSelectionControlled
    ? (selectedId as string | null)
    : internalSelectedId

  const isModeControlled = mode !== undefined
  const [internalMode, setInternalMode] = React.useState<VisualAnnotatorMode>(
    defaultMode,
  )
  const effectiveMode = isModeControlled ? (mode as VisualAnnotatorMode) : internalMode

  // ─ Refs + pending-draft state ─────────────────────────────────────────
  const overlayRef = React.useRef<HTMLDivElement | null>(null)
  const [draft, setDraft] = React.useState<PendingDraft | null>(null)
  const [sending, setSending] = React.useState<boolean>(false)

  // ─ Rect helpers ───────────────────────────────────────────────────────
  const readOverlayRect = React.useCallback((): OverlayRect | null => {
    if (getOverlayRect) return getOverlayRect()
    const el = overlayRef.current
    if (!el) return null
    const r = el.getBoundingClientRect()
    return { left: r.left, top: r.top, width: r.width, height: r.height }
  }, [getOverlayRect])

  const applyAnnotationsUpdate = React.useCallback(
    (updater: (prev: MobileVisualAnnotation[]) => MobileVisualAnnotation[]) => {
      const base = isAnnotationsControlled
        ? (annotations as MobileVisualAnnotation[])
        : internalAnnotations
      const next = updater(base)
      if (!isAnnotationsControlled) setInternalAnnotations(next)
      onAnnotationsChange?.(next)
    },
    [annotations, internalAnnotations, isAnnotationsControlled, onAnnotationsChange],
  )

  const applySelectionUpdate = React.useCallback(
    (next: string | null) => {
      if (!isSelectionControlled) setInternalSelectedId(next)
      if (next !== effectiveSelectedId) onSelectionChange?.(next)
    },
    [effectiveSelectedId, isSelectionControlled, onSelectionChange],
  )

  const changeMode = React.useCallback(
    (next: VisualAnnotatorMode) => {
      if (!isModeControlled) setInternalMode(next)
      if (next !== effectiveMode) onModeChange?.(next)
    },
    [effectiveMode, isModeControlled, onModeChange],
  )

  // ─ Pointer gestures ───────────────────────────────────────────────────
  const pointerToLocal = React.useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      const rect = readOverlayRect()
      if (!rect || rect.width <= 0 || rect.height <= 0) {
        return { local: null, rect: null }
      }
      return {
        local: { x: event.clientX - rect.left, y: event.clientY - rect.top },
        rect,
      }
    },
    [readOverlayRect],
  )

  const commitAnnotation = React.useCallback(
    (type: VisualAnnotationType, box: NormalizedBoundingBox) => {
      const ts = nowIso()
      const id = idFactory()
      const annotation: MobileVisualAnnotation = {
        id,
        type,
        boundingBox: box,
        comment: "",
        cssSelector: null,
        componentHint: null,
        createdAt: ts,
        updatedAt: ts,
      }
      applyAnnotationsUpdate((prev) => [
        ...prev,
        { ...annotation, label: prev.length + 1 },
      ])
      applySelectionUpdate(id)
    },
    [applyAnnotationsUpdate, applySelectionUpdate, idFactory, nowIso],
  )

  const handlePointerDown = React.useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      if (disabled) return
      if (event.button !== 0) return
      const { local, rect } = pointerToLocal(event)
      if (!local || !rect) return

      if (effectiveMode === "rect") {
        event.preventDefault()
        setDraft({
          startX: local.x,
          startY: local.y,
          currentX: local.x,
          currentY: local.y,
          pointerId: event.pointerId,
        })
        return
      }
      if (effectiveMode === "click") {
        const box = pointsToNormalizedBox(local, local, rect)
        commitAnnotation("click", { x: box.x, y: box.y, w: 0, h: 0 })
        return
      }
      if (effectiveMode === "select") {
        const nx = local.x / rect.width
        const ny = local.y / rect.height
        const hit = [...effectiveAnnotations]
          .reverse()
          .find((a) => hitTestNormalizedBox(a.boundingBox, nx, ny))
        applySelectionUpdate(hit ? hit.id : null)
      }
    },
    [
      applySelectionUpdate,
      commitAnnotation,
      disabled,
      effectiveAnnotations,
      effectiveMode,
      pointerToLocal,
    ],
  )

  const handlePointerMove = React.useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      if (!draft) return
      if (event.pointerId !== draft.pointerId) return
      const { local } = pointerToLocal(event)
      if (!local) return
      setDraft((prev) =>
        prev ? { ...prev, currentX: local.x, currentY: local.y } : prev,
      )
    },
    [draft, pointerToLocal],
  )

  const finishDraft = React.useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      if (!draft) return
      if (event.pointerId !== draft.pointerId) return
      const rect = readOverlayRect()
      if (!rect) {
        setDraft(null)
        return
      }
      const box = pointsToNormalizedBox(
        { x: draft.startX, y: draft.startY },
        { x: draft.currentX, y: draft.currentY },
        rect,
      )
      setDraft(null)
      if (box.w < rectMinNormalized && box.h < rectMinNormalized) {
        commitAnnotation("click", { x: box.x, y: box.y, w: 0, h: 0 })
        return
      }
      commitAnnotation("rect", box)
    },
    [commitAnnotation, draft, readOverlayRect, rectMinNormalized],
  )

  // ─ Annotation mutations ───────────────────────────────────────────────
  const updateComment = React.useCallback(
    (id: string, comment: string) => {
      const ts = nowIso()
      applyAnnotationsUpdate((prev) =>
        prev.map((a) => (a.id === id ? { ...a, comment, updatedAt: ts } : a)),
      )
    },
    [applyAnnotationsUpdate, nowIso],
  )

  const updateComponentHint = React.useCallback(
    (id: string, hint: string) => {
      const ts = nowIso()
      const trimmed = hint.trim()
      applyAnnotationsUpdate((prev) =>
        prev.map((a) =>
          a.id === id
            ? { ...a, componentHint: trimmed.length === 0 ? null : trimmed, updatedAt: ts }
            : a,
        ),
      )
    },
    [applyAnnotationsUpdate, nowIso],
  )

  const removeAnnotation = React.useCallback(
    (id: string) => {
      applyAnnotationsUpdate((prev) =>
        prev.filter((a) => a.id !== id).map((a, i) => ({ ...a, label: i + 1 })),
      )
      if (effectiveSelectedId === id) applySelectionUpdate(null)
    },
    [applyAnnotationsUpdate, applySelectionUpdate, effectiveSelectedId],
  )

  const clearAll = React.useCallback(() => {
    applyAnnotationsUpdate(() => [])
    applySelectionUpdate(null)
  }, [applyAnnotationsUpdate, applySelectionUpdate])

  // ─ Derived data ───────────────────────────────────────────────────────
  const labelledAnnotations = React.useMemo(
    () => effectiveAnnotations.map((a, i) => ({ ...a, label: a.label ?? i + 1 })),
    [effectiveAnnotations],
  )

  const selectedAnnotation =
    labelledAnnotations.find((a) => a.id === effectiveSelectedId) ?? null

  React.useEffect(() => {
    if (effectiveSelectedId === null) return
    const stillThere = effectiveAnnotations.some((a) => a.id === effectiveSelectedId)
    if (!stillThere) applySelectionUpdate(null)
  }, [applySelectionUpdate, effectiveAnnotations, effectiveSelectedId])

  const handleKeyDown = React.useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      if (disabled) return
      if (!effectiveSelectedId) return
      if (event.key !== "Delete" && event.key !== "Backspace") return
      const target = event.target as HTMLElement | null
      if (target && (target.tagName === "TEXTAREA" || target.tagName === "INPUT")) return
      event.preventDefault()
      removeAnnotation(effectiveSelectedId)
    },
    [disabled, effectiveSelectedId, removeAnnotation],
  )

  const handleSend = React.useCallback(async () => {
    if (!onSendToAgent) return
    if (labelledAnnotations.length === 0) return
    const payloads = toMobileAgentPayloads(labelledAnnotations, platform, device)
    setSending(true)
    try {
      await onSendToAgent(payloads)
    } finally {
      setSending(false)
    }
  }, [device, labelledAnnotations, onSendToAgent, platform])

  // ─ Draft rectangle (preview while dragging) ───────────────────────────
  const draftBox = React.useMemo<NormalizedBoundingBox | null>(() => {
    if (!draft) return null
    const rect = readOverlayRect()
    if (!rect) return null
    return pointsToNormalizedBox(
      { x: draft.startX, y: draft.startY },
      { x: draft.currentX, y: draft.currentY },
      rect,
    )
  }, [draft, readOverlayRect])

  const cursorClass =
    effectiveMode === "rect"
      ? "cursor-crosshair"
      : effectiveMode === "click"
        ? "cursor-pointer"
        : "cursor-default"

  // Outer bezel / screen geometry for the overlay positioning.
  // `DeviceFrame` computes the screen rectangle relative to the
  // caller's `width` prop, so we mirror the same math here to pin the
  // overlay over the inner screen — *not* the bezel.  This keeps the
  // gesture surface aligned with the pixels the agent cares about.
  const scale = React.useMemo(() => {
    const outerW = profile.screenWidth + profile.bezel.left + profile.bezel.right
    if (outerW <= 0) return 0
    return frameWidth / outerW
  }, [frameWidth, profile])

  const screenCssLeft = profile.bezel.left * scale
  const screenCssTop = profile.bezel.top * scale
  const screenCssWidth = profile.screenWidth * scale
  const screenCssHeight = profile.screenHeight * scale

  return (
    <section
      data-testid={testId ?? "mobile-visual-annotator"}
      data-mode={effectiveMode}
      data-platform={platform}
      data-framework={framework}
      data-device={device}
      data-disabled={disabled ? "true" : "false"}
      aria-label="Mobile visual annotator"
      className={cn(
        "flex min-h-0 w-full flex-col overflow-hidden rounded-md border border-border bg-card/40",
        className,
      )}
    >
      <header
        data-testid="mobile-visual-annotator-toolbar"
        className="flex h-9 shrink-0 items-center justify-between gap-2 border-b border-border px-3"
      >
        <span className="truncate text-xs font-semibold uppercase tracking-wider text-muted-foreground">
          {profile.label} · {framework}
        </span>
        <div className="flex items-center gap-1">
          <ToolbarButton
            mode="rect"
            active={effectiveMode === "rect"}
            disabled={disabled}
            onClick={() => changeMode("rect")}
            icon={<Square className="size-3.5" aria-hidden="true" />}
          />
          <ToolbarButton
            mode="click"
            active={effectiveMode === "click"}
            disabled={disabled}
            onClick={() => changeMode("click")}
            icon={<Target className="size-3.5" aria-hidden="true" />}
          />
          <ToolbarButton
            mode="select"
            active={effectiveMode === "select"}
            disabled={disabled}
            onClick={() => changeMode("select")}
            icon={<MousePointer2 className="size-3.5" aria-hidden="true" />}
          />
          <Button
            type="button"
            variant="ghost"
            size="sm"
            data-testid="mobile-visual-annotator-clear"
            aria-label="Clear all annotations"
            disabled={disabled || labelledAnnotations.length === 0}
            onClick={clearAll}
            className="h-7 px-2 text-xs"
          >
            <Trash2 className="mr-1 size-3.5" aria-hidden="true" />
            Clear
          </Button>
        </div>
      </header>

      <div
        data-testid="mobile-visual-annotator-stage"
        className="flex flex-1 items-start justify-center overflow-auto bg-muted/30 p-4"
      >
        <div
          className="relative"
          style={{ width: frameWidth }}
          data-testid="mobile-visual-annotator-frame-wrap"
        >
          <DeviceFrame
            device={device}
            screenshotUrl={screenshotUrl}
            alt={screenshotAlt ?? `${profile.label} screenshot`}
            width={frameWidth}
            empty={!screenshotUrl}
            data-testid="mobile-visual-annotator-device-frame"
          />
          <div
            ref={overlayRef}
            data-testid="mobile-visual-annotator-surface"
            data-drafting={draft ? "true" : "false"}
            role="application"
            tabIndex={disabled ? -1 : 0}
            aria-label={`Mobile annotation canvas — ${MODE_LABEL[effectiveMode]}`}
            onPointerDown={handlePointerDown}
            onPointerMove={handlePointerMove}
            onPointerUp={finishDraft}
            onPointerCancel={finishDraft}
            onKeyDown={handleKeyDown}
            className={cn(
              "absolute select-none touch-none outline-none",
              cursorClass,
              disabled && "pointer-events-none opacity-60",
            )}
            style={{
              left: screenCssLeft,
              top: screenCssTop,
              width: screenCssWidth,
              height: screenCssHeight,
            }}
          >
            {labelledAnnotations.map((a) => {
              const active = a.id === effectiveSelectedId
              const style: React.CSSProperties =
                a.type === "rect"
                  ? {
                      left: `${a.boundingBox.x * 100}%`,
                      top: `${a.boundingBox.y * 100}%`,
                      width: `${a.boundingBox.w * 100}%`,
                      height: `${a.boundingBox.h * 100}%`,
                    }
                  : {
                      left: `${a.boundingBox.x * 100}%`,
                      top: `${a.boundingBox.y * 100}%`,
                    }
              return (
                <div
                  key={a.id}
                  data-testid={`mobile-visual-annotator-annotation-${a.id}`}
                  data-annotation-type={a.type}
                  data-active={active ? "true" : "false"}
                  data-has-comment={a.comment.length > 0 ? "true" : "false"}
                  data-has-hint={a.componentHint ? "true" : "false"}
                  style={style}
                  className={cn(
                    "absolute",
                    a.type === "rect"
                      ? "rounded-sm border-2"
                      : "-translate-x-1/2 -translate-y-1/2",
                    a.type === "rect" &&
                      (active
                        ? "border-primary bg-primary/20"
                        : "border-emerald-400/80 bg-emerald-400/15"),
                  )}
                >
                  <span
                    data-testid={`mobile-visual-annotator-label-${a.id}`}
                    className={cn(
                      "absolute -left-1 -top-5 rounded-sm px-1 text-[10px] font-semibold",
                      active
                        ? "bg-primary text-primary-foreground"
                        : "bg-emerald-400 text-emerald-950",
                      a.type === "click" &&
                        "left-auto top-auto -translate-y-1/2 translate-x-2 -ml-1",
                    )}
                  >
                    #{a.label}
                  </span>
                  {a.type === "click" && (
                    <span
                      aria-hidden="true"
                      className={cn(
                        "block size-3 rounded-full border-2 bg-background",
                        active ? "border-primary" : "border-emerald-400",
                      )}
                    />
                  )}
                </div>
              )
            })}

            {draftBox && draftBox.w > 0 && draftBox.h > 0 && (
              <div
                data-testid="mobile-visual-annotator-draft"
                style={{
                  left: `${draftBox.x * 100}%`,
                  top: `${draftBox.y * 100}%`,
                  width: `${draftBox.w * 100}%`,
                  height: `${draftBox.h * 100}%`,
                }}
                className="pointer-events-none absolute rounded-sm border-2 border-dashed border-primary bg-primary/10"
              />
            )}
          </div>
        </div>
      </div>

      <footer
        data-testid="mobile-visual-annotator-footer"
        className="flex shrink-0 flex-col gap-2 border-t border-border px-3 py-2"
      >
        <div
          data-testid="mobile-visual-annotator-summary"
          className="flex items-center justify-between gap-2 text-[11px] text-muted-foreground"
        >
          <span>
            {labelledAnnotations.length === 0
              ? `No annotations yet — pick a tool and mark the ${profile.label} screenshot.`
              : `${labelledAnnotations.length} annotation${
                  labelledAnnotations.length === 1 ? "" : "s"
                } · target ${framework} (${fileExt})`}
          </span>
          <span
            data-testid="mobile-visual-annotator-mode-label"
            className="uppercase tracking-wider"
          >
            {MODE_LABEL[effectiveMode]}
          </span>
        </div>

        {selectedAnnotation && (
          <div
            data-testid="mobile-visual-annotator-editor"
            data-editor-for={selectedAnnotation.id}
            className="flex flex-col gap-1 rounded-md border border-border bg-background/60 p-2"
          >
            <div className="flex items-center justify-between gap-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
              <span>
                #{selectedAnnotation.label} · {selectedAnnotation.type} · {framework}
              </span>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                data-testid={`mobile-visual-annotator-remove-${selectedAnnotation.id}`}
                aria-label={`Remove annotation ${selectedAnnotation.label}`}
                disabled={disabled}
                onClick={() => removeAnnotation(selectedAnnotation.id)}
                className="size-6"
              >
                <X className="size-3" aria-hidden="true" />
              </Button>
            </div>
            <input
              type="text"
              data-testid={`mobile-visual-annotator-hint-${selectedAnnotation.id}`}
              aria-label={`Component hint for annotation ${selectedAnnotation.label}`}
              placeholder={
                framework === "swiftui"
                  ? "Accessibility identifier (e.g. 'sendButton')"
                  : framework === "jetpack-compose"
                    ? "testTag or @Composable name (e.g. 'SendButton')"
                    : framework === "flutter"
                      ? "Widget Key or class (e.g. 'SendButton')"
                      : "testID or component name (e.g. 'SendButton')"
              }
              value={selectedAnnotation.componentHint ?? ""}
              disabled={disabled}
              onChange={(e) => updateComponentHint(selectedAnnotation.id, e.target.value)}
              className="h-7 rounded-md border border-border bg-background px-2 text-xs outline-none focus:border-primary focus-visible:border-primary"
            />
            <Textarea
              data-testid={`mobile-visual-annotator-comment-${selectedAnnotation.id}`}
              aria-label={`Comment for annotation ${selectedAnnotation.label}`}
              placeholder={`Tell the agent what to change — e.g. "Make this button use the primary tint."`}
              value={selectedAnnotation.comment}
              disabled={disabled}
              onChange={(e) => updateComment(selectedAnnotation.id, e.target.value)}
              rows={2}
              className="min-h-[44px] resize-none text-xs"
            />
          </div>
        )}

        {onSendToAgent && (
          <Button
            type="button"
            size="sm"
            variant="secondary"
            data-testid="mobile-visual-annotator-send"
            disabled={disabled || sending || labelledAnnotations.length === 0}
            onClick={handleSend}
            className="h-7 self-end px-3 text-xs"
          >
            {sending ? "Sending…" : `Send ${labelledAnnotations.length} to ${framework}`}
          </Button>
        )}
      </footer>
    </section>
  )
}

// ─── Local sub-component ───────────────────────────────────────────────────

interface ToolbarButtonProps {
  mode: VisualAnnotatorMode
  active: boolean
  disabled: boolean
  onClick: () => void
  icon: React.ReactNode
}

function ToolbarButton({ mode, active, disabled, onClick, icon }: ToolbarButtonProps) {
  return (
    <Button
      type="button"
      variant={active ? "default" : "ghost"}
      size="sm"
      data-testid={`mobile-visual-annotator-mode-${mode}`}
      data-active={active ? "true" : "false"}
      aria-pressed={active}
      aria-label={MODE_LABEL[mode]}
      disabled={disabled}
      onClick={onClick}
      className="h-7 px-2 text-xs"
    >
      {icon}
      <span className="ml-1 capitalize">{mode}</span>
    </Button>
  )
}

export default MobileVisualAnnotator
