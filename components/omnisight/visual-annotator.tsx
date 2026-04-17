/**
 * V3 #1 (TODO row 1520) — Visual annotation overlay.
 *
 * A self-contained overlay placed on top of a Web-workspace preview
 * screenshot so the operator can direct the next agent iteration with
 * high-signal feedback instead of a paragraph of prose:
 *
 *   - Draw a **rectangle** around a region that needs attention
 *     ("make this card narrower"). The rectangle is persisted as a
 *     normalised bounding box so it survives image re-flow.
 *   - **Click** a specific element ("this button is too small"). Click
 *     annotations carry a zero-sized bounding box and, when the caller
 *     wires up the upcoming V3 #3 element inspector, an optional
 *     `cssSelector`.
 *   - **Comment** on any existing annotation so the handoff to the
 *     next ReAct turn is textually explicit ("wrong colour, should
 *     match brand primary").
 *
 * This is only V3 checkbox **#1** — the overlay itself.  Two adjacent
 * V3 checkboxes consume the data shape we produce here:
 *
 *   - V3 #2 (annotation → agent context) will serialise each
 *     `VisualAnnotation` into the `{type, cssSelector, boundingBox,
 *     comment}` payload that rides the next ReAct prompt.  We lock in
 *     that exact field layout now so #2 becomes a pure transform.
 *   - V3 #3 (element inspector) will populate the `cssSelector` field
 *     via `data-omnisight-component` attributes injected into the
 *     sandbox React tree.  Until then, `cssSelector` is null — the
 *     overlay never fabricates one.
 *
 * Coordinate model:
 *   All bounding boxes are in **normalised** image coordinates
 *   `(x, y, w, h) ∈ [0, 1]` — fractions of the underlying screenshot.
 *   This means a box stays anchored to the same pixel region even if
 *   the overlay is resized (responsive layout, devtools pane resize,
 *   etc.).  The caller is free to scale back to source pixels using
 *   the screenshot's intrinsic width × height.
 *
 * Controlled + uncontrolled:
 *   Matches the rest of the workspace component family —
 *   `annotations` pins the list (controlled) and `defaultAnnotations`
 *   seeds an internal list (uncontrolled).  Ditto `selectedId` /
 *   `defaultSelectedId` and `mode` / `defaultMode`.  Controlled callers
 *   receive every mutation via `onAnnotationsChange` but are entirely
 *   responsible for reflecting it back through the `annotations` prop.
 *
 * Why a single overlay instead of three separate widgets:
 *   Rect / click / comment share the same affordances — hit-testing,
 *   selection model, inline comment editor, keyboard delete — and
 *   splitting them would multiply the surface tests and leave the
 *   three-mode operator flow (toggle mode → draw → comment) scattered
 *   across components.  The toolbar picks the active mode; everything
 *   else is shared.
 */
"use client"

import * as React from "react"
import { MousePointer2, Square, Target, Trash2, X } from "lucide-react"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"

// ─── Public shapes ─────────────────────────────────────────────────────────

export type VisualAnnotationType = "click" | "rect"

export type VisualAnnotatorMode = "rect" | "click" | "select"

export interface NormalizedBoundingBox {
  /** Left edge in normalised image coordinates [0, 1]. */
  x: number
  /** Top edge in normalised image coordinates [0, 1]. */
  y: number
  /** Width in normalised image coordinates [0, 1] — zero for click points. */
  w: number
  /** Height in normalised image coordinates [0, 1] — zero for click points. */
  h: number
}

export interface VisualAnnotation {
  id: string
  /** Discriminator matching the V3 #2 agent-context payload. */
  type: VisualAnnotationType
  /** Normalised bounding box (click points use `{w:0, h:0}`). */
  boundingBox: NormalizedBoundingBox
  /** Operator comment — may be empty, never undefined. */
  comment: string
  /**
   * CSS selector resolved by the V3 #3 element inspector, when present.
   * The overlay itself never populates this field — callers who have
   * an inspector attached may inject one per-annotation.
   */
  cssSelector?: string | null
  /** Auto-assigned 1-based label (shown in the overlay). */
  label?: number
  /** ISO-8601 of the create event. */
  createdAt: string
  /** ISO-8601 of the last mutation (comment edit or selector update). */
  updatedAt: string
}

export interface OverlayRect {
  left: number
  top: number
  width: number
  height: number
}

export interface VisualAnnotatorProps {
  /** URL of the screenshot to annotate (e.g. sandbox preview frame). */
  imageSrc: string
  /** Alt text — defaults to a neutral "Preview screenshot". */
  imageAlt?: string
  /** Controlled annotation list. */
  annotations?: VisualAnnotation[]
  /** Uncontrolled initial annotations. */
  defaultAnnotations?: VisualAnnotation[]
  /** Fired after every annotation mutation (create / update / delete). */
  onAnnotationsChange?: (next: VisualAnnotation[]) => void
  /** Controlled selection id. */
  selectedId?: string | null
  /** Uncontrolled initial selection. */
  defaultSelectedId?: string | null
  /** Fired on every selection change (including null). */
  onSelectionChange?: (id: string | null) => void
  /** Controlled active mode (`rect` / `click` / `select`). */
  mode?: VisualAnnotatorMode
  /** Uncontrolled initial mode — defaults to `"rect"`. */
  defaultMode?: VisualAnnotatorMode
  /** Fired when the user picks a different mode from the toolbar. */
  onModeChange?: (mode: VisualAnnotatorMode) => void
  /**
   * When drawing a rectangle, the caller-side **minimum** normalised
   * size below which the gesture is promoted into a click point
   * instead.  Defaults to `0.01` (1 % of image size on either axis).
   */
  rectMinNormalized?: number
  /** Disable all mutation surfaces (read-only overlay). */
  disabled?: boolean
  /** Factory for new annotation ids — swap out in tests for determinism. */
  idFactory?: () => string
  /** Clock seam for `createdAt` / `updatedAt`. */
  nowIso?: () => string
  /**
   * Test-only seam: return the current overlay rect in client-space
   * pixels.  Defaults to `element.getBoundingClientRect()`, which
   * returns zeros in jsdom — tests should inject a real rect.
   */
  getOverlayRect?: () => OverlayRect
  /** Override the root div class. */
  className?: string
}

// ─── Defaults ──────────────────────────────────────────────────────────────

const DEFAULT_RECT_MIN_NORMALIZED = 0.01

const MODE_LABEL: Record<VisualAnnotatorMode, string> = {
  rect: "Draw rectangle",
  click: "Pin click point",
  select: "Select annotation",
}

/**
 * Stable id factory that works under jsdom (which does not always
 * ship `crypto.randomUUID`).  Exported so tests can drive it
 * directly and cover the fallback branch.
 */
export function defaultAnnotatorIdFactory(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID()
  }
  return `ann-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

export function defaultAnnotatorNowIso(): string {
  return new Date().toISOString()
}

// ─── Pure helpers (exported for test coverage) ────────────────────────────

/** Clamp a number into `[0, 1]` — robust against NaN / Infinity. */
export function clampNormalized(n: number): number {
  if (!Number.isFinite(n)) return 0
  if (n < 0) return 0
  if (n > 1) return 1
  return n
}

/**
 * Convert two points (pointerdown + pointerup) in overlay-client pixels
 * into a normalised `NormalizedBoundingBox`.  The caller passes the
 * overlay's rect — so this is a pure function that tests can drive
 * without rendering the DOM.
 */
export function pointsToNormalizedBox(
  a: { x: number; y: number },
  b: { x: number; y: number },
  rect: Pick<OverlayRect, "width" | "height">,
): NormalizedBoundingBox {
  if (rect.width <= 0 || rect.height <= 0) {
    return { x: 0, y: 0, w: 0, h: 0 }
  }
  const nax = clampNormalized(a.x / rect.width)
  const nay = clampNormalized(a.y / rect.height)
  const nbx = clampNormalized(b.x / rect.width)
  const nby = clampNormalized(b.y / rect.height)
  const x = Math.min(nax, nbx)
  const y = Math.min(nay, nby)
  const w = Math.abs(nbx - nax)
  const h = Math.abs(nby - nay)
  return { x, y, w, h }
}

/**
 * Test whether a normalised point `(nx, ny)` falls inside a box.  Used
 * by the overlay's hit-test when clicking an existing annotation in
 * `select` mode.  Click annotations (w=0, h=0) still hit on their
 * centre — we add a small epsilon so you can actually click them.
 */
export function hitTestNormalizedBox(
  box: NormalizedBoundingBox,
  nx: number,
  ny: number,
  epsilon = 0.015,
): boolean {
  const eps = Math.max(0, epsilon)
  const minX = box.x - (box.w === 0 ? eps : 0)
  const minY = box.y - (box.h === 0 ? eps : 0)
  const maxX = box.x + Math.max(box.w, 0) + (box.w === 0 ? eps : 0)
  const maxY = box.y + Math.max(box.h, 0) + (box.h === 0 ? eps : 0)
  return nx >= minX && nx <= maxX && ny >= minY && ny <= maxY
}

/**
 * Serialise an annotation into the V3 #2 agent-context payload.
 * Exposed here so the next checkbox (annotation → agent context) is a
 * pure transform consumers can import without reaching into overlay
 * internals.  Kept deliberately side-effect-free and schema-stable.
 */
export interface VisualAnnotationAgentPayload {
  type: VisualAnnotationType
  cssSelector: string | null
  boundingBox: NormalizedBoundingBox
  comment: string
}

export function annotationToAgentPayload(
  annotation: VisualAnnotation,
): VisualAnnotationAgentPayload {
  return {
    type: annotation.type,
    cssSelector: annotation.cssSelector ?? null,
    boundingBox: { ...annotation.boundingBox },
    comment: annotation.comment,
  }
}

// ─── Component ─────────────────────────────────────────────────────────────

interface PendingDraft {
  startX: number
  startY: number
  currentX: number
  currentY: number
  pointerId: number
}

export function VisualAnnotator({
  imageSrc,
  imageAlt = "Preview screenshot",
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
  disabled = false,
  idFactory = defaultAnnotatorIdFactory,
  nowIso = defaultAnnotatorNowIso,
  getOverlayRect,
  className,
}: VisualAnnotatorProps) {
  // ─ Controlled / uncontrolled state wiring ───────────────────────────────
  const isAnnotationsControlled = annotations !== undefined
  const [internalAnnotations, setInternalAnnotations] = React.useState<VisualAnnotation[]>(
    () => (defaultAnnotations ? defaultAnnotations.map((a) => ({ ...a })) : []),
  )
  const effectiveAnnotations = isAnnotationsControlled
    ? (annotations as VisualAnnotation[])
    : internalAnnotations

  const isSelectionControlled = selectedId !== undefined
  const [internalSelectedId, setInternalSelectedId] = React.useState<string | null>(
    defaultSelectedId ?? null,
  )
  const effectiveSelectedId = isSelectionControlled
    ? (selectedId as string | null)
    : internalSelectedId

  const isModeControlled = mode !== undefined
  const [internalMode, setInternalMode] = React.useState<VisualAnnotatorMode>(defaultMode)
  const effectiveMode = isModeControlled ? (mode as VisualAnnotatorMode) : internalMode

  // ─ Refs + pending-draft state ───────────────────────────────────────────
  const overlayRef = React.useRef<HTMLDivElement | null>(null)
  const [draft, setDraft] = React.useState<PendingDraft | null>(null)

  // ─ Rect helpers ────────────────────────────────────────────────────────
  const readOverlayRect = React.useCallback((): OverlayRect | null => {
    if (getOverlayRect) return getOverlayRect()
    const el = overlayRef.current
    if (!el) return null
    const r = el.getBoundingClientRect()
    return { left: r.left, top: r.top, width: r.width, height: r.height }
  }, [getOverlayRect])

  const applyAnnotationsUpdate = React.useCallback(
    (updater: (prev: VisualAnnotation[]) => VisualAnnotation[]) => {
      const base = isAnnotationsControlled
        ? (annotations as VisualAnnotation[])
        : internalAnnotations
      const next = updater(base)
      if (!isAnnotationsControlled) {
        setInternalAnnotations(next)
      }
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

  // ─ Pointer gestures ────────────────────────────────────────────────────
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
      const annotation: VisualAnnotation = {
        id,
        type,
        boundingBox: box,
        comment: "",
        cssSelector: null,
        createdAt: ts,
        updatedAt: ts,
      }
      applyAnnotationsUpdate((prev) => {
        const next = [...prev, { ...annotation, label: prev.length + 1 }]
        return next
      })
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
        // Iterate last-to-first so later (visually on top) annotations win.
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
      // Tiny gestures become click points (matches V3 #2 payload shape).
      if (box.w < rectMinNormalized && box.h < rectMinNormalized) {
        commitAnnotation("click", { x: box.x, y: box.y, w: 0, h: 0 })
        return
      }
      commitAnnotation("rect", box)
    },
    [commitAnnotation, draft, readOverlayRect, rectMinNormalized],
  )

  // ─ Annotation mutations ────────────────────────────────────────────────
  const updateComment = React.useCallback(
    (id: string, comment: string) => {
      const ts = nowIso()
      applyAnnotationsUpdate((prev) =>
        prev.map((a) => (a.id === id ? { ...a, comment, updatedAt: ts } : a)),
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

  // ─ Derived data ────────────────────────────────────────────────────────
  const labelledAnnotations = React.useMemo(
    () => effectiveAnnotations.map((a, i) => ({ ...a, label: a.label ?? i + 1 })),
    [effectiveAnnotations],
  )

  const selectedAnnotation =
    labelledAnnotations.find((a) => a.id === effectiveSelectedId) ?? null

  // Drop selection if the selected annotation disappears (controlled caller).
  React.useEffect(() => {
    if (effectiveSelectedId === null) return
    const stillThere = effectiveAnnotations.some((a) => a.id === effectiveSelectedId)
    if (!stillThere) applySelectionUpdate(null)
  }, [applySelectionUpdate, effectiveAnnotations, effectiveSelectedId])

  // Keyboard: Delete / Backspace removes selected annotation when overlay focused.
  const handleKeyDown = React.useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      if (disabled) return
      if (!effectiveSelectedId) return
      if (event.key !== "Delete" && event.key !== "Backspace") return
      const target = event.target as HTMLElement | null
      // Don't steal keystrokes from the comment editor.
      if (target && (target.tagName === "TEXTAREA" || target.tagName === "INPUT")) return
      event.preventDefault()
      removeAnnotation(effectiveSelectedId)
    },
    [disabled, effectiveSelectedId, removeAnnotation],
  )

  // ─ Render ──────────────────────────────────────────────────────────────
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

  return (
    <section
      data-testid="visual-annotator"
      data-mode={effectiveMode}
      data-disabled={disabled ? "true" : "false"}
      aria-label="Visual annotator"
      className={cn(
        "flex min-h-0 w-full flex-col overflow-hidden rounded-md border border-border bg-card/40",
        className,
      )}
    >
      <header
        data-testid="visual-annotator-toolbar"
        className="flex h-9 shrink-0 items-center justify-between gap-2 border-b border-border px-3"
      >
        <span className="truncate text-xs font-semibold uppercase tracking-wider text-muted-foreground">
          Annotate preview
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
            data-testid="visual-annotator-clear"
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
        ref={overlayRef}
        data-testid="visual-annotator-surface"
        data-drafting={draft ? "true" : "false"}
        role="application"
        tabIndex={disabled ? -1 : 0}
        aria-label={`Annotation canvas — ${MODE_LABEL[effectiveMode]}`}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={finishDraft}
        onPointerCancel={finishDraft}
        onKeyDown={handleKeyDown}
        className={cn(
          "relative flex-1 select-none touch-none outline-none",
          cursorClass,
          disabled && "opacity-60",
        )}
      >
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={imageSrc}
          alt={imageAlt}
          draggable={false}
          data-testid="visual-annotator-image"
          className="pointer-events-none block h-full w-full object-contain"
        />

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
              data-testid={`visual-annotator-annotation-${a.id}`}
              data-annotation-type={a.type}
              data-active={active ? "true" : "false"}
              data-has-comment={a.comment.length > 0 ? "true" : "false"}
              style={style}
              className={cn(
                "absolute",
                a.type === "rect"
                  ? "rounded-sm border-2"
                  : "-translate-x-1/2 -translate-y-1/2",
                a.type === "rect" &&
                  (active ? "border-primary bg-primary/10" : "border-amber-400/80 bg-amber-400/10"),
              )}
            >
              <span
                data-testid={`visual-annotator-label-${a.id}`}
                className={cn(
                  "absolute -left-1 -top-5 rounded-sm px-1 text-[10px] font-semibold",
                  active
                    ? "bg-primary text-primary-foreground"
                    : "bg-amber-400 text-amber-950",
                  a.type === "click" && "left-auto top-auto -translate-y-1/2 translate-x-2 -ml-1",
                )}
              >
                #{a.label}
              </span>
              {a.type === "click" && (
                <span
                  aria-hidden="true"
                  className={cn(
                    "block size-3 rounded-full border-2 bg-background",
                    active ? "border-primary" : "border-amber-400",
                  )}
                />
              )}
            </div>
          )
        })}

        {draftBox && draftBox.w > 0 && draftBox.h > 0 && (
          <div
            data-testid="visual-annotator-draft"
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

      <footer
        data-testid="visual-annotator-footer"
        className="flex shrink-0 flex-col gap-2 border-t border-border px-3 py-2"
      >
        <div
          data-testid="visual-annotator-summary"
          className="flex items-center justify-between gap-2 text-[11px] text-muted-foreground"
        >
          <span>
            {labelledAnnotations.length === 0
              ? "No annotations yet — pick a tool and mark the preview."
              : `${labelledAnnotations.length} annotation${
                  labelledAnnotations.length === 1 ? "" : "s"
                }`}
          </span>
          <span data-testid="visual-annotator-mode-label" className="uppercase tracking-wider">
            {MODE_LABEL[effectiveMode]}
          </span>
        </div>

        {selectedAnnotation && (
          <div
            data-testid="visual-annotator-editor"
            data-editor-for={selectedAnnotation.id}
            className="flex flex-col gap-1 rounded-md border border-border bg-background/60 p-2"
          >
            <div className="flex items-center justify-between gap-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
              <span>
                #{selectedAnnotation.label} · {selectedAnnotation.type}
              </span>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                data-testid={`visual-annotator-remove-${selectedAnnotation.id}`}
                aria-label={`Remove annotation ${selectedAnnotation.label}`}
                disabled={disabled}
                onClick={() => removeAnnotation(selectedAnnotation.id)}
                className="size-6"
              >
                <X className="size-3" aria-hidden="true" />
              </Button>
            </div>
            <Textarea
              data-testid={`visual-annotator-comment-${selectedAnnotation.id}`}
              aria-label={`Comment for annotation ${selectedAnnotation.label}`}
              placeholder="Add a comment — the agent reads this on the next turn."
              value={selectedAnnotation.comment}
              disabled={disabled}
              onChange={(e) => updateComment(selectedAnnotation.id, e.target.value)}
              rows={2}
              className="min-h-[44px] resize-none text-xs"
            />
          </div>
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
      data-testid={`visual-annotator-mode-${mode}`}
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

export default VisualAnnotator
