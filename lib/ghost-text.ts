/**
 * OP-1506 / WP.12 — Ghost-text suggestion helpers for the chat composer.
 *
 * Pattern inspired by Warp's multiline editor + AI-suggestion flag
 * (`crates/editor/src/multiline.rs`), independently implemented here as
 * pure helpers so the React overlay layer in
 * `components/omnisight/chat-ghost-text.tsx` stays a thin shell.
 *
 * Scope: end-of-input completion only — ghost-text always conceptually
 * appends to the current draft. Tab accepts the whole tail; Ctrl-→
 * accepts the next word boundary; Esc dismisses; Alt-] cycles to the
 * next alternative when the caller supplied a tuple of suggestions.
 *
 * No third-party editor library is pulled in — the design doc tags
 * WP.12 as Tier 3 / "inspiration-only", and adding @codemirror/* to the
 * lockfile is out-of-scope for a polish ticket.
 */

/**
 * Normalize the caller-supplied suggestion payload into a stable
 * tuple of non-empty strings.  Accepts:
 *   - `null` / `undefined`  → []
 *   - empty / blank string  → []
 *   - single string         → [string]
 *   - array of strings      → filtered to non-empty entries (in order)
 *
 * Trimming is *not* applied — leading whitespace in a suggestion is
 * meaningful (e.g. " continue" so the accepted text reads
 * "hello continue").
 */
export function normalizeSuggestions(
  input: string | string[] | null | undefined,
): string[] {
  if (input == null) return []
  if (typeof input === "string") {
    return input.length > 0 ? [input] : []
  }
  return input.filter((s): s is string => typeof s === "string" && s.length > 0)
}

/**
 * Match the leading whitespace run + the first word of `suggestion`.
 * Word := a maximal run of non-whitespace characters.  Returns the
 * empty string when the suggestion has neither leading whitespace nor
 * a word (i.e. the caller passed something exotic like ``""``).
 *
 * This is the unit Ctrl-→ accepts.  Splitting on word boundary keeps
 * parity with shell-style line editors where Alt-F / Ctrl-→ advance by
 * one word.
 */
export function nextWordSlice(suggestion: string): string {
  if (!suggestion) return ""
  // Leading whitespace run.
  let i = 0
  while (i < suggestion.length && /\s/.test(suggestion[i]!)) i += 1
  // Then a run of non-whitespace.
  while (i < suggestion.length && !/\s/.test(suggestion[i]!)) i += 1
  return suggestion.slice(0, i)
}

/**
 * Apply Tab — accept the entire ghost suggestion.  Returns the new
 * composer text and the empty remainder.  Caller is responsible for
 * clearing the suggestion list afterwards.
 */
export function acceptGhostAll(
  text: string,
  suggestion: string,
): { text: string; remainder: string } {
  if (!suggestion) return { text, remainder: "" }
  return { text: text + suggestion, remainder: "" }
}

/**
 * Apply Ctrl-→ — accept the next word slice of the suggestion.
 * Returns the new composer text and the leftover suggestion the
 * caller should re-render as ghost-text on the next keystroke.
 */
export function acceptGhostWord(
  text: string,
  suggestion: string,
): { text: string; remainder: string } {
  const slice = nextWordSlice(suggestion)
  if (!slice) return { text, remainder: suggestion }
  return { text: text + slice, remainder: suggestion.slice(slice.length) }
}

/**
 * Apply Alt-] — cycle to the next alternative suggestion.  Wraps
 * around the end of the tuple so repeated presses keep cycling.
 * Returns the input index unchanged when the tuple is empty or a
 * singleton (no point switching).
 */
export function cycleAlternative(current: number, count: number): number {
  if (count <= 1) return current
  const normalized = ((current % count) + count) % count
  return (normalized + 1) % count
}

/**
 * Identify which keyboard event we are looking at.  Returned tags
 * drive the ChatGhostText keydown handler.  Plain Enter is *not*
 * tagged here — the chat composer's submit binding owns that key, and
 * the ghost-text overlay should never swallow submission.
 */
export type GhostKeyAction =
  | "accept-all"
  | "accept-word"
  | "dismiss"
  | "cycle-alt"
  | null

interface GhostKeyEvent {
  key: string
  ctrlKey?: boolean
  metaKey?: boolean
  altKey?: boolean
  shiftKey?: boolean
}

/**
 * Classify a keyboard event for ghost-text handling.  Pure so the
 * overlay component can call it without a synthetic event, and the
 * test suite can drive it with a plain object literal.
 *
 * Binding table:
 *   - Tab (no modifiers)            → accept-all
 *   - Ctrl-→ / Meta-→ (mac parity)  → accept-word
 *   - Escape                        → dismiss
 *   - Alt-]                         → cycle-alt
 *
 * Mac note: macOS users routinely substitute Cmd for Ctrl in editor
 * shortcuts; honour `metaKey` for parity.
 */
export function classifyGhostKey(event: GhostKeyEvent): GhostKeyAction {
  if (event.key === "Tab" && !event.shiftKey && !event.ctrlKey && !event.metaKey && !event.altKey) {
    return "accept-all"
  }
  if (event.key === "ArrowRight" && (event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey) {
    return "accept-word"
  }
  if (event.key === "Escape" && !event.shiftKey && !event.ctrlKey && !event.metaKey && !event.altKey) {
    return "dismiss"
  }
  if (event.key === "]" && event.altKey && !event.shiftKey && !event.ctrlKey && !event.metaKey) {
    return "cycle-alt"
  }
  return null
}
