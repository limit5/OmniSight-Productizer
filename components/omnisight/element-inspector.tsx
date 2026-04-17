/**
 * V3 #3 (TODO row 1523) — Element inspector integration.
 *
 * Lightweight React-DevTools stand-in for the Web-workspace preview.
 * The sandbox React tree is expected to carry two opt-in data
 * attributes on any node the operator should be able to inspect:
 *
 *   - `data-omnisight-component="ComponentName"` — display label.
 *   - `data-omnisight-props='{"title":"Hi","variant":"primary"}'` —
 *     JSON-encoded props snapshot (optional).  Malformed JSON is
 *     surfaced as a parse error rather than silently dropped so the
 *     injection toolchain can be iterated on.
 *
 * On hover, the inspector walks up from the pointer target to the
 * nearest `[data-omnisight-component]` ancestor and renders a floating
 * panel with three sections:
 *
 *   1. **Component name** — from the attribute.
 *   2. **Current props** — key/value pairs, values truncated so a big
 *      prop blob does not blow out the panel.
 *   3. **Computed styles** — a filtered subset of `getComputedStyle()`
 *      (display, position, sizing, typography, box model).  The full
 *      style declaration is rarely useful; the curated allowlist
 *      mirrors what an operator tweaks when telling the agent "make
 *      this narrower / bold / more padded".
 *
 * Click pins the current inspection so the operator can read the panel
 * without keeping the cursor in place; clicking the same element a
 * second time (or pressing Escape) unpins.  The inspector emits the
 * pinned element's V3 #1-compatible `cssSelector` via `onPin`, letting
 * the surrounding `VisualAnnotator` populate its currently-null
 * `cssSelector` field without reaching into inspector internals.
 *
 * This is checkbox **V3 #3** — the overlay itself.  The matching
 * sandbox-side transformer that actually injects
 * `data-omnisight-component` attributes is an orthogonal tool (Babel
 * plugin / SWC transform) that will ship alongside the V4 sandbox
 * integration work.  The inspector is deliberately defensive: if an
 * element carries no `data-omnisight-component`, it is simply ignored
 * — the inspector never fabricates a component name.
 *
 * Controlled + uncontrolled:
 *   Matches the rest of the workspace component family —
 *   `hoveredInspection` / `defaultHoveredInspection` and
 *   `pinnedInspection` / `defaultPinnedInspection` + their
 *   `on*Change` callbacks.
 */
"use client"

import * as React from "react"
import { Pin, PinOff, X } from "lucide-react"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"

// ─── Public shapes ─────────────────────────────────────────────────────────

/** Attribute the sandbox React tree is expected to emit. */
export const OMNISIGHT_COMPONENT_ATTR = "data-omnisight-component"
/** Optional sibling attribute carrying a JSON-encoded props snapshot. */
export const OMNISIGHT_PROPS_ATTR = "data-omnisight-props"

/**
 * Curated subset of `CSSStyleDeclaration` surfaced by the inspector.
 * Ordered so the panel reads top-to-bottom the way an operator thinks
 * about tweaks: layout → size → typography → box model.  Exported so
 * callers can extend the allowlist (e.g. add `grid-template-columns`)
 * without patching the component.
 */
export const DEFAULT_COMPUTED_STYLE_KEYS = [
  "display",
  "position",
  "width",
  "height",
  "color",
  "backgroundColor",
  "fontSize",
  "fontFamily",
  "fontWeight",
  "margin",
  "padding",
  "border",
  "borderRadius",
] as const

export type ComputedStyleKey = (typeof DEFAULT_COMPUTED_STYLE_KEYS)[number]

export interface ElementComputedStyles {
  [key: string]: string
}

export interface InspectionBoundingBox {
  left: number
  top: number
  width: number
  height: number
}

export interface ElementInspection {
  /** Reference to the DOM node that carries `data-omnisight-component`. */
  element: HTMLElement
  /** Component name read verbatim from the attribute. */
  componentName: string
  /**
   * Parsed props. Always an object — non-object JSON (array, number,
   * string) yields an empty object and a parse error.
   */
  props: Record<string, unknown>
  /** Raw attribute value (null if the attribute was absent). */
  propsRaw: string | null
  /** Human-readable parse error, or null when parsing succeeded. */
  parseError: string | null
  /** Curated subset of computed styles. */
  computedStyles: ElementComputedStyles
  /**
   * CSS selector stable enough to survive a re-render of the same
   * component.  Used by V3 #1 `VisualAnnotator` to populate its
   * currently-null `cssSelector` field.
   */
  cssSelector: string
  /** Element bounding rect — overlay chrome uses this to position badges. */
  boundingBox: InspectionBoundingBox
}

export interface ElementInspectorProps {
  /** Rendered React tree that the inspector observes. */
  children?: React.ReactNode
  /** Turn the inspector off (pointer events still pass through). */
  disabled?: boolean
  /** Controlled hovered inspection. */
  hoveredInspection?: ElementInspection | null
  /** Uncontrolled initial hovered inspection. */
  defaultHoveredInspection?: ElementInspection | null
  /** Fires on every hover change (enter / leave / switch). */
  onHoveredChange?: (next: ElementInspection | null) => void
  /** Controlled pinned inspection. */
  pinnedInspection?: ElementInspection | null
  /** Uncontrolled initial pinned inspection. */
  defaultPinnedInspection?: ElementInspection | null
  /** Fires on every pin / unpin. */
  onPinnedChange?: (next: ElementInspection | null) => void
  /**
   * Allowlist of computed-style keys to surface.  Defaults to
   * `DEFAULT_COMPUTED_STYLE_KEYS`; callers can extend or replace.
   */
  computedStyleKeys?: readonly string[]
  /**
   * Upper bound on prop-value display length — longer values are
   * truncated with an ellipsis so the panel stays readable.
   */
  propValueMaxChars?: number
  /** Override the root div class. */
  className?: string
  /**
   * Test seam: return a `CSSStyleDeclaration` for the given element.
   * Defaults to `window.getComputedStyle`, which jsdom ships — but
   * tests can inject a deterministic stub to exercise rendering.
   */
  getComputedStyleImpl?: (el: Element) => CSSStyleDeclaration
  /**
   * Test seam: return the element's bounding rect.  jsdom returns
   * zeros without layout; tests inject a fixed rect.
   */
  getBoundingClientRectImpl?: (el: Element) => DOMRect | InspectionBoundingBox
}

// ─── Pure helpers (exported for test coverage) ────────────────────────────

/**
 * Walk `target`'s ancestor chain up to (but not past) `root`, returning
 * the first element that carries `data-omnisight-component`.  Returns
 * null when no ancestor matches — which is the common path for
 * sandboxes that have not yet been instrumented.
 */
export function findNearestOmnisightAncestor(
  target: HTMLElement | null,
  root?: HTMLElement | null,
): HTMLElement | null {
  let cursor: HTMLElement | null = target
  while (cursor) {
    if (cursor.hasAttribute(OMNISIGHT_COMPONENT_ATTR)) return cursor
    if (root && cursor === root) return null
    cursor = cursor.parentElement
  }
  return null
}

/**
 * Parse the `data-omnisight-props` attribute.  Returns `{props, error}`
 * so the UI can show "props unavailable (parse error)" instead of
 * pretending the element has no props.
 */
export function parseOmnisightProps(
  raw: string | null | undefined,
): { props: Record<string, unknown>; error: string | null } {
  if (raw === null || raw === undefined) return { props: {}, error: null }
  if (raw === "") return { props: {}, error: null }
  try {
    const parsed = JSON.parse(raw) as unknown
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      return {
        props: {},
        error: `Expected JSON object, got ${Array.isArray(parsed) ? "array" : typeof parsed}`,
      }
    }
    return { props: parsed as Record<string, unknown>, error: null }
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err)
    return { props: {}, error: `Invalid JSON: ${message}` }
  }
}

/**
 * Pick a stable subset of a `CSSStyleDeclaration` into a plain object.
 * Missing keys become empty strings so the inspector can show
 * "display: (unset)" instead of crashing.
 */
export function pickComputedStyles(
  style: CSSStyleDeclaration | { getPropertyValue?: (k: string) => string } | null | undefined,
  keys: readonly string[] = DEFAULT_COMPUTED_STYLE_KEYS,
): ElementComputedStyles {
  const out: ElementComputedStyles = {}
  for (const key of keys) {
    if (!style) {
      out[key] = ""
      continue
    }
    // Try direct property access first (CSSStyleDeclaration + jsdom both support this).
    const direct = (style as unknown as Record<string, unknown>)[key]
    if (typeof direct === "string" && direct.length > 0) {
      out[key] = direct
      continue
    }
    if (typeof (style as CSSStyleDeclaration).getPropertyValue === "function") {
      // getPropertyValue wants kebab-case.
      const kebab = key.replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`)
      const value = (style as CSSStyleDeclaration).getPropertyValue(kebab) ?? ""
      out[key] = typeof value === "string" ? value : ""
      continue
    }
    out[key] = ""
  }
  return out
}

/**
 * Truncate a prop value into a single-line string fit for the panel.
 * Strings are quoted; objects / arrays are JSON-stringified; primitives
 * pass through `String()`.  `maxChars` defaults to 80; `0` disables
 * truncation.
 */
export function formatPropValue(value: unknown, maxChars = 80): string {
  let text: string
  if (typeof value === "string") text = JSON.stringify(value)
  else if (value === null) text = "null"
  else if (value === undefined) text = "undefined"
  else if (typeof value === "number" || typeof value === "boolean") text = String(value)
  else if (typeof value === "function") text = "ƒ ()"
  else {
    try {
      text = JSON.stringify(value)
    } catch {
      text = Object.prototype.toString.call(value)
    }
    if (text === undefined) text = String(value)
  }
  if (maxChars <= 0) return text
  if (text.length <= maxChars) return text
  return `${text.slice(0, Math.max(1, maxChars - 1))}…`
}

/**
 * Compute a selector rooted on `data-omnisight-component` so it
 * remains stable across re-renders.  When multiple siblings share the
 * same component name we append `:nth-of-type(…)`; the selector also
 * includes the component's own id / class when present so operators
 * see an identifiable path.
 */
export function computeOmnisightSelector(
  element: HTMLElement,
  root?: HTMLElement | null,
): string {
  const name = element.getAttribute(OMNISIGHT_COMPONENT_ATTR)
  if (!name) return ""
  const parts: string[] = []
  let cursor: HTMLElement | null = element
  while (cursor && cursor !== root) {
    const componentName = cursor.getAttribute(OMNISIGHT_COMPONENT_ATTR)
    if (!componentName) {
      cursor = cursor.parentElement
      continue
    }
    let segment = `[${OMNISIGHT_COMPONENT_ATTR}="${componentName}"]`
    // Disambiguate with nth-of-type when siblings share the same component.
    const parent = cursor.parentElement
    if (parent) {
      const siblings = Array.from(parent.children).filter(
        (c) => c.getAttribute(OMNISIGHT_COMPONENT_ATTR) === componentName,
      )
      if (siblings.length > 1) {
        const idx = siblings.indexOf(cursor) + 1
        segment += `:nth-of-type(${idx})`
      }
    }
    parts.unshift(segment)
    cursor = cursor.parentElement
  }
  return parts.join(" > ")
}

/**
 * Pure inspection entry-point so callers can test hover payloads
 * without mounting the component.
 */
export interface InspectOptions {
  root?: HTMLElement | null
  computedStyleKeys?: readonly string[]
  getComputedStyleImpl?: (el: Element) => CSSStyleDeclaration
  getBoundingClientRectImpl?: (el: Element) => DOMRect | InspectionBoundingBox
}

export function inspectElement(
  element: HTMLElement,
  opts: InspectOptions = {},
): ElementInspection | null {
  if (!element.hasAttribute(OMNISIGHT_COMPONENT_ATTR)) return null
  const componentName = element.getAttribute(OMNISIGHT_COMPONENT_ATTR) ?? ""
  const propsRaw = element.getAttribute(OMNISIGHT_PROPS_ATTR)
  const { props, error } = parseOmnisightProps(propsRaw)
  const styleFn = opts.getComputedStyleImpl ?? defaultGetComputedStyle
  const style = styleFn(element)
  const computedStyles = pickComputedStyles(
    style,
    opts.computedStyleKeys ?? DEFAULT_COMPUTED_STYLE_KEYS,
  )
  const rectFn = opts.getBoundingClientRectImpl ?? defaultGetBoundingRect
  const rect = rectFn(element)
  const boundingBox: InspectionBoundingBox = {
    left: rect.left ?? 0,
    top: rect.top ?? 0,
    width: rect.width ?? 0,
    height: rect.height ?? 0,
  }
  const cssSelector = computeOmnisightSelector(element, opts.root ?? null)
  return {
    element,
    componentName,
    props,
    propsRaw,
    parseError: error,
    computedStyles,
    cssSelector,
    boundingBox,
  }
}

function defaultGetComputedStyle(el: Element): CSSStyleDeclaration {
  if (typeof window === "undefined") {
    return {} as CSSStyleDeclaration
  }
  return window.getComputedStyle(el)
}

function defaultGetBoundingRect(el: Element): DOMRect {
  return el.getBoundingClientRect()
}

// ─── Component ─────────────────────────────────────────────────────────────

const DEFAULT_PROP_VALUE_MAX_CHARS = 80

export function ElementInspector({
  children,
  disabled = false,
  hoveredInspection,
  defaultHoveredInspection,
  onHoveredChange,
  pinnedInspection,
  defaultPinnedInspection,
  onPinnedChange,
  computedStyleKeys = DEFAULT_COMPUTED_STYLE_KEYS,
  propValueMaxChars = DEFAULT_PROP_VALUE_MAX_CHARS,
  className,
  getComputedStyleImpl,
  getBoundingClientRectImpl,
}: ElementInspectorProps) {
  const rootRef = React.useRef<HTMLDivElement | null>(null)

  // ─ Controlled / uncontrolled state wiring ───────────────────────────────
  const isHoveredControlled = hoveredInspection !== undefined
  const [internalHovered, setInternalHovered] = React.useState<ElementInspection | null>(
    defaultHoveredInspection ?? null,
  )
  const effectiveHovered = isHoveredControlled
    ? (hoveredInspection as ElementInspection | null)
    : internalHovered

  const isPinnedControlled = pinnedInspection !== undefined
  const [internalPinned, setInternalPinned] = React.useState<ElementInspection | null>(
    defaultPinnedInspection ?? null,
  )
  const effectivePinned = isPinnedControlled
    ? (pinnedInspection as ElementInspection | null)
    : internalPinned

  const applyHoveredUpdate = React.useCallback(
    (next: ElementInspection | null) => {
      if (!isHoveredControlled) setInternalHovered(next)
      if ((effectiveHovered?.element ?? null) !== (next?.element ?? null)) {
        onHoveredChange?.(next)
      }
    },
    [effectiveHovered, isHoveredControlled, onHoveredChange],
  )

  const applyPinnedUpdate = React.useCallback(
    (next: ElementInspection | null) => {
      if (!isPinnedControlled) setInternalPinned(next)
      if ((effectivePinned?.element ?? null) !== (next?.element ?? null)) {
        onPinnedChange?.(next)
      }
    },
    [effectivePinned, isPinnedControlled, onPinnedChange],
  )

  // ─ Inspection builder (picks up the nearest ancestor from an event) ────
  const buildInspectionFor = React.useCallback(
    (rawTarget: EventTarget | null): ElementInspection | null => {
      if (!(rawTarget instanceof Element)) return null
      const asHtml = rawTarget as HTMLElement
      const nearest = findNearestOmnisightAncestor(asHtml, rootRef.current)
      if (!nearest) return null
      return inspectElement(nearest, {
        root: rootRef.current,
        computedStyleKeys,
        getComputedStyleImpl,
        getBoundingClientRectImpl,
      })
    },
    [computedStyleKeys, getBoundingClientRectImpl, getComputedStyleImpl],
  )

  // ─ Pointer handlers ────────────────────────────────────────────────────
  const handlePointerOver = React.useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      if (disabled) return
      const inspection = buildInspectionFor(event.target)
      if (!inspection) {
        if (effectiveHovered !== null) applyHoveredUpdate(null)
        return
      }
      if (effectiveHovered?.element === inspection.element) return
      applyHoveredUpdate(inspection)
    },
    [applyHoveredUpdate, buildInspectionFor, disabled, effectiveHovered],
  )

  const handlePointerLeave = React.useCallback(() => {
    if (disabled) return
    if (effectiveHovered !== null) applyHoveredUpdate(null)
  }, [applyHoveredUpdate, disabled, effectiveHovered])

  const handleClick = React.useCallback(
    (event: React.MouseEvent<HTMLDivElement>) => {
      if (disabled) return
      const inspection = buildInspectionFor(event.target)
      if (!inspection) return
      // Clicking the already-pinned element unpins; any other click pins.
      if (effectivePinned?.element === inspection.element) {
        applyPinnedUpdate(null)
        return
      }
      applyPinnedUpdate(inspection)
    },
    [applyPinnedUpdate, buildInspectionFor, disabled, effectivePinned],
  )

  const handleKeyDown = React.useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      if (disabled) return
      if (event.key !== "Escape") return
      if (effectivePinned === null && effectiveHovered === null) return
      event.preventDefault()
      // Escape fully closes the panel — hovered pane otherwise keeps
      // the inspector visible even after the operator tried to dismiss it.
      if (effectivePinned !== null) applyPinnedUpdate(null)
      if (effectiveHovered !== null) applyHoveredUpdate(null)
    },
    [applyHoveredUpdate, applyPinnedUpdate, disabled, effectiveHovered, effectivePinned],
  )

  // Drop hovered / pinned inspections if the underlying element leaves
  // the DOM (e.g. parent re-rendered and swapped nodes).
  React.useEffect(() => {
    if (effectiveHovered && !effectiveHovered.element.isConnected) {
      applyHoveredUpdate(null)
    }
  }, [applyHoveredUpdate, effectiveHovered])
  React.useEffect(() => {
    if (effectivePinned && !effectivePinned.element.isConnected) {
      applyPinnedUpdate(null)
    }
  }, [applyPinnedUpdate, effectivePinned])

  // ─ Derived panel content ───────────────────────────────────────────────
  // Pinned inspection wins the panel — it represents the operator's
  // explicit selection and must not flicker when the cursor moves over
  // a sibling element.
  const panelInspection = effectivePinned ?? effectiveHovered
  const isPinned = panelInspection !== null && panelInspection === effectivePinned

  return (
    <div
      ref={rootRef}
      data-testid="element-inspector"
      data-disabled={disabled ? "true" : "false"}
      data-has-hovered={effectiveHovered ? "true" : "false"}
      data-has-pinned={effectivePinned ? "true" : "false"}
      role="group"
      aria-label="Element inspector"
      tabIndex={disabled ? -1 : 0}
      onPointerOver={handlePointerOver}
      onPointerLeave={handlePointerLeave}
      onClick={handleClick}
      onKeyDown={handleKeyDown}
      className={cn(
        "relative flex min-h-0 w-full flex-col overflow-hidden rounded-md border border-border bg-card/40 outline-none",
        disabled && "opacity-60",
        className,
      )}
    >
      <div
        data-testid="element-inspector-viewport"
        className="relative flex-1 min-h-0 overflow-auto"
      >
        {children}
      </div>

      {panelInspection && (
        <InspectorPanel
          inspection={panelInspection}
          pinned={isPinned}
          disabled={disabled}
          propValueMaxChars={propValueMaxChars}
          onUnpin={() => applyPinnedUpdate(null)}
          onPin={() => applyPinnedUpdate(panelInspection)}
        />
      )}
    </div>
  )
}

// ─── Panel sub-component ──────────────────────────────────────────────────

interface InspectorPanelProps {
  inspection: ElementInspection
  pinned: boolean
  disabled: boolean
  propValueMaxChars: number
  onUnpin: () => void
  onPin: () => void
}

function InspectorPanel({
  inspection,
  pinned,
  disabled,
  propValueMaxChars,
  onUnpin,
  onPin,
}: InspectorPanelProps) {
  const propEntries = Object.entries(inspection.props)
  const styleEntries = Object.entries(inspection.computedStyles)

  return (
    <aside
      data-testid="element-inspector-panel"
      data-pinned={pinned ? "true" : "false"}
      data-component={inspection.componentName}
      aria-label={`Inspector for ${inspection.componentName}`}
      className="shrink-0 border-t border-border bg-background/80 p-2 text-xs"
    >
      <header className="flex items-center justify-between gap-2 pb-2">
        <div className="flex min-w-0 items-center gap-2">
          <Badge
            data-testid="element-inspector-component-name"
            variant="secondary"
            className="font-mono text-[11px]"
          >
            {inspection.componentName}
          </Badge>
          <span
            data-testid="element-inspector-selector"
            className="truncate font-mono text-[10px] text-muted-foreground"
            title={inspection.cssSelector}
          >
            {inspection.cssSelector}
          </span>
        </div>
        <div className="flex items-center gap-1">
          {pinned ? (
            <Button
              type="button"
              variant="ghost"
              size="icon"
              data-testid="element-inspector-unpin"
              aria-label="Unpin inspector"
              disabled={disabled}
              onClick={(e) => {
                e.stopPropagation()
                onUnpin()
              }}
              className="size-6"
            >
              <PinOff className="size-3" aria-hidden="true" />
            </Button>
          ) : (
            <Button
              type="button"
              variant="ghost"
              size="icon"
              data-testid="element-inspector-pin"
              aria-label="Pin inspector"
              disabled={disabled}
              onClick={(e) => {
                e.stopPropagation()
                onPin()
              }}
              className="size-6"
            >
              <Pin className="size-3" aria-hidden="true" />
            </Button>
          )}
          {pinned && (
            <Button
              type="button"
              variant="ghost"
              size="icon"
              data-testid="element-inspector-close"
              aria-label="Close inspector"
              disabled={disabled}
              onClick={(e) => {
                e.stopPropagation()
                onUnpin()
              }}
              className="size-6"
            >
              <X className="size-3" aria-hidden="true" />
            </Button>
          )}
        </div>
      </header>

      <section
        data-testid="element-inspector-props"
        data-has-parse-error={inspection.parseError ? "true" : "false"}
        className="flex flex-col gap-1 border-t border-border pt-2"
      >
        <span className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
          Props
        </span>
        {inspection.parseError ? (
          <span
            data-testid="element-inspector-props-error"
            className="text-[11px] text-destructive"
          >
            {inspection.parseError}
          </span>
        ) : propEntries.length === 0 ? (
          <span
            data-testid="element-inspector-props-empty"
            className="text-[11px] text-muted-foreground"
          >
            (no props)
          </span>
        ) : (
          <dl className="grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5">
            {propEntries.map(([key, value]) => (
              <React.Fragment key={key}>
                <dt
                  data-testid={`element-inspector-prop-key-${key}`}
                  className="font-mono text-[10px] text-muted-foreground"
                >
                  {key}
                </dt>
                <dd
                  data-testid={`element-inspector-prop-value-${key}`}
                  className="truncate font-mono text-[10px]"
                  title={formatPropValue(value, 0)}
                >
                  {formatPropValue(value, propValueMaxChars)}
                </dd>
              </React.Fragment>
            ))}
          </dl>
        )}
      </section>

      <section
        data-testid="element-inspector-styles"
        className="mt-2 flex flex-col gap-1 border-t border-border pt-2"
      >
        <span className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
          Computed styles
        </span>
        {styleEntries.length === 0 ? (
          <span className="text-[11px] text-muted-foreground">(no styles)</span>
        ) : (
          <dl className="grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5">
            {styleEntries.map(([key, value]) => (
              <React.Fragment key={key}>
                <dt
                  data-testid={`element-inspector-style-key-${key}`}
                  className="font-mono text-[10px] text-muted-foreground"
                >
                  {key}
                </dt>
                <dd
                  data-testid={`element-inspector-style-value-${key}`}
                  className="truncate font-mono text-[10px]"
                  title={value || "(unset)"}
                >
                  {value || "(unset)"}
                </dd>
              </React.Fragment>
            ))}
          </dl>
        )}
      </section>
    </aside>
  )
}

export default ElementInspector
