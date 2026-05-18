/**
 * OP-1506 / WP.12 — Ghost-text suggestion overlay for the chat composer.
 *
 * Pattern inspired by Warp's multiline editor + AI suggestion flag
 * (`crates/editor/src/multiline.rs`) — independently implemented; no
 * Warp source is referenced.  WP.12 sits in the "Inspiration-only"
 * tier of `docs/legal/oss-boundaries.md` so we re-create the UX
 * (inline gray ghost-text + Tab/Ctrl-→/Esc/Alt-] keyboard table)
 * without depending on CodeMirror 6 or any editor library.
 *
 * Why a hand-rolled overlay instead of CodeMirror 6:
 *   1. The chat composer is a single-paragraph plain-text affair.  A
 *      full editor framework is overkill and forces a ~70 KB-min add
 *      to the FE bundle, which conflicts with the "Tier 3 / 0-2 day
 *      polish" budget in the design doc.
 *   2. The Inspiration-only tier explicitly forbids vendor-port style
 *      adoption — the moral spirit is "borrow the pattern, write our
 *      own".  Adding @codemirror/* would also touch the lockfile, an
 *      out-of-area domain for this ticket (frontend + tests only).
 *
 * Composition (mirror technique):
 *   - The host `<textarea>` keeps owning text input + caret.  We wrap
 *     it in a relatively-positioned container together with an
 *     absolutely-positioned mirror `<div>` styled identically (font,
 *     padding, line-height, wrap).  The mirror renders the live
 *     value invisibly so the ghost suffix lands at the natural caret
 *     position when the cursor sits at end-of-input, which is the
 *     only case ghost-text is shown (per WP.12 spec).
 *   - When the suggestion is non-empty, the mirror appends a
 *     `<span data-testid="chat-ghost-suggestion">` with muted colour
 *     showing the suggestion text.
 *
 * Keyboard table (delegated to {@link classifyGhostKey}):
 *   - Tab            → accept the whole suggestion
 *   - Ctrl-→ / ⌘-→  → accept the next word slice
 *   - Escape         → dismiss the suggestion
 *   - Alt-]          → cycle to the next alternative
 *
 * The component is fully controlled — it never owns the draft text or
 * the suggestions; callers wire those via props.  This keeps the
 * provider-resolution / debounce / network-fetch story in the
 * composer (or its host) rather than here, mirroring the
 * `WorkspaceChat` "caller owns plumbing" contract.
 */
"use client"

import * as React from "react"
import { cn } from "@/lib/utils"
import { Textarea } from "@/components/ui/textarea"
import {
  acceptGhostAll,
  acceptGhostWord,
  classifyGhostKey,
  cycleAlternative,
  normalizeSuggestions,
} from "@/lib/ghost-text"

export interface ChatGhostTextProps
  extends Omit<
    React.ComponentProps<"textarea">,
    "value" | "onChange" | "onKeyDown"
  > {
  /** Controlled composer text — required so accept handlers can update it. */
  value: string
  /** Controlled-text setter — invoked on user keystrokes and on accept. */
  onChange: (next: string) => void
  /**
   * Suggestion payload — `null`/`""`/`[]` disables ghost-text entirely.
   * Multiple entries enable Alt-] cycling.  Caller-owned so the
   * provider/debounce/cancel story lives in the composer.
   */
  suggestions?: string | string[] | null
  /**
   * Active-alternative index override.  When omitted the component
   * cycles internally — supply a value only when the caller wants to
   * reset cycling on each new suggestion batch.
   */
  alternativeIndex?: number
  /** Notified whenever the active alternative index changes. */
  onAlternativeIndexChange?: (next: number) => void
  /** Notified when the operator accepts the whole suggestion (Tab). */
  onAcceptAll?: (acceptedText: string) => void
  /** Notified when the operator accepts a single word (Ctrl-→). */
  onAcceptWord?: (acceptedText: string) => void
  /** Notified when the operator dismisses (Esc). */
  onDismiss?: () => void
  /** Forwarded to the inner textarea after ghost-text handling. */
  onKeyDownPassthrough?: React.KeyboardEventHandler<HTMLTextAreaElement>
  className?: string
}

/**
 * Stable styles applied to BOTH the textarea and the mirror so the
 * ghost-text lands at the same visual position as the caret.  Keep
 * these in sync — divergence here is the classic "ghost-text floats
 * off the caret" bug.
 */
const SHARED_TYPOGRAPHY_CLASSES =
  "min-h-[48px] w-full resize-none px-3 py-2 text-base md:text-sm leading-normal"

export function ChatGhostText({
  value,
  onChange,
  suggestions = null,
  alternativeIndex,
  onAlternativeIndexChange,
  onAcceptAll,
  onAcceptWord,
  onDismiss,
  onKeyDownPassthrough,
  className,
  disabled,
  ...rest
}: ChatGhostTextProps) {
  const textareaProps = rest as React.ComponentProps<"textarea"> & {
    "data-testid"?: string
    className?: string
  }
  const normalized = React.useMemo(
    () => normalizeSuggestions(suggestions),
    [suggestions],
  )
  const [internalIndex, setInternalIndex] = React.useState<number>(0)
  // Reset the internal index whenever the suggestion batch identity
  // changes — a new batch should always start at the first option,
  // even if the previous index would still be in bounds.  Comparing
  // by reference works because callers either replace the prop or
  // pass `null`; mutating an existing array in place is unsupported.
  const previousSuggestionsRef = React.useRef<string | string[] | null | undefined>(suggestions)
  React.useEffect(() => {
    if (previousSuggestionsRef.current !== suggestions) {
      previousSuggestionsRef.current = suggestions
      setInternalIndex(0)
    }
  }, [suggestions])

  const activeIndex = (() => {
    if (typeof alternativeIndex === "number" && normalized.length > 0) {
      const len = normalized.length
      return ((alternativeIndex % len) + len) % len
    }
    if (normalized.length === 0) return 0
    return Math.min(internalIndex, normalized.length - 1)
  })()
  const activeSuggestion = normalized[activeIndex] ?? ""

  const setActiveIndex = React.useCallback(
    (next: number) => {
      if (typeof alternativeIndex !== "number") {
        setInternalIndex(next)
      }
      onAlternativeIndexChange?.(next)
    },
    [alternativeIndex, onAlternativeIndexChange],
  )

  const handleKeyDown = React.useCallback(
    (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
      const ghostAction = activeSuggestion ? classifyGhostKey(event) : null
      if (ghostAction === "accept-all") {
        event.preventDefault()
        const { text } = acceptGhostAll(value, activeSuggestion)
        onChange(text)
        onAcceptAll?.(activeSuggestion)
        return
      }
      if (ghostAction === "accept-word") {
        event.preventDefault()
        const { text } = acceptGhostWord(value, activeSuggestion)
        // The accepted slice = diff between new and old text.
        const acceptedSlice = text.slice(value.length)
        onChange(text)
        if (acceptedSlice) onAcceptWord?.(acceptedSlice)
        return
      }
      if (ghostAction === "dismiss") {
        event.preventDefault()
        onDismiss?.()
        return
      }
      if (ghostAction === "cycle-alt") {
        event.preventDefault()
        if (normalized.length > 1) {
          const nextIdx = cycleAlternative(activeIndex, normalized.length)
          setActiveIndex(nextIdx)
        }
        return
      }
      onKeyDownPassthrough?.(event)
    },
    [
      activeIndex,
      activeSuggestion,
      normalized.length,
      onAcceptAll,
      onAcceptWord,
      onChange,
      onDismiss,
      onKeyDownPassthrough,
      setActiveIndex,
      value,
    ],
  )

  const showGhost = Boolean(activeSuggestion) && !disabled

  return (
    <div
      data-testid="chat-ghost-text-container"
      data-has-suggestion={showGhost ? "true" : "false"}
      data-alternative-count={normalized.length}
      data-alternative-index={activeIndex}
      className={cn("relative flex-1", className)}
    >
      {showGhost && (
        <div
          aria-hidden="true"
          data-testid="chat-ghost-text-mirror"
          className={cn(
            "pointer-events-none absolute inset-0 whitespace-pre-wrap break-words text-transparent",
            SHARED_TYPOGRAPHY_CLASSES,
          )}
        >
          {value}
          <span
            data-testid="chat-ghost-suggestion"
            className="text-muted-foreground/60"
          >
            {activeSuggestion}
          </span>
        </div>
      )}
      <Textarea
        {...textareaProps}
        data-testid={textareaProps["data-testid"] ?? "chat-ghost-textarea"}
        disabled={disabled}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={handleKeyDown}
        className={cn(
          "relative bg-transparent",
          SHARED_TYPOGRAPHY_CLASSES,
          textareaProps.className,
        )}
      />
    </div>
  )
}

export default ChatGhostText
