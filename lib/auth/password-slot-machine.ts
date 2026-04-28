/**
 * AS.7.2 — Password slot-machine state machine.
 *
 * The signup page ships a "🎲 generate password" button that plays
 * a quantum-collapse animation when pressed:
 *
 *    1. **Cycle phase** (200ms): every column scrolls through random
 *       glyphs at ~24fps. Visually identical to a casino slot reel.
 *    2. **Collapse phase** (~600ms total): columns lock left-to-right
 *       with a 30ms stagger. Each column scale-flashes (1.0 → 1.18
 *       → 1.0) the moment it locks, then settles.
 *    3. **Settled phase**: the final password is shown; the value
 *       is copied to clipboard / browser keychain prompt is fired.
 *
 * The `<PasswordSlotMachine>` React leaf consumes this module; the
 * helper is a pure state-machine reducer so the animation timing
 * is fully testable without a real DOM. The vitest contract test
 * drives `tickSlotMachine()` through the deterministic timeline
 * and asserts the per-column lock order + glyph distribution.
 *
 * Module-global state audit (per implement_phase_step.md SOP §1):
 *   - All exports are pure functions or `as const` constants. The
 *     state object is opaque to the caller and treated immutably.
 *   - The "random glyph cycle" uses a counter-driven deterministic
 *     index (not Math.random) so the displayed reel is reproducible
 *     in tests and free of cross-tab divergence (Answer #1 of the
 *     SOP §1 audit).
 *   - The final password comes from the AS.0.10 password-generator
 *     library (which DOES use Web Crypto). The slot-machine helper
 *     itself never generates the password — it just animates the
 *     reveal of a pre-generated string.
 *
 * Read-after-write timing audit: N/A — pure state-machine helper,
 * no DB calls, no parallelisation change.
 */

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Animation timing constants
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Total cycle phase duration before the first column locks (ms). */
export const SLOT_CYCLE_DURATION_MS = 200

/** Per-column stagger during the collapse phase (ms). */
export const SLOT_COLLAPSE_STAGGER_MS = 30

/** How long each column scale-flashes after it locks (ms). */
export const SLOT_LOCK_FLASH_MS = 180

/** Recommended cycle-frame interval (ms). Drives the rAF loop the
 *  React leaf runs during the cycle phase. 24fps → ~42ms. */
export const SLOT_CYCLE_FRAME_MS = 42

/** Maximum length the slot machine can animate. Tunable so the
 *  slot reel doesn't blow up on a 128-char Style-A password. The
 *  cycle / collapse only animates up to this length; if the
 *  generated password is longer, columns past the cap show their
 *  final glyph immediately on phase 0. */
export const SLOT_MAX_ANIMATED_COLUMNS = 24

/** Glyph pool used during the cycle phase. Rich enough to look
 *  chaotic but small enough to render quickly (32 chars → 5 bits
 *  of "look-busy" entropy per frame, deterministic in tests). */
export const SLOT_CYCLE_GLYPHS =
  "ABCDEFGHJKLMNPQRSTUVWXYZ23456789#$%&"

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  State shape
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export type SlotPhase = "idle" | "cycle" | "collapse" | "settled"

export interface SlotMachineState {
  readonly phase: SlotPhase
  /** Final password string the animation is collapsing onto. */
  readonly target: string
  /** Per-column visible glyph (length = `target.length`). */
  readonly columns: readonly string[]
  /** Per-column lock state — true once the column has settled on
   *  its final glyph. Once every column is true the phase flips
   *  to `settled`. */
  readonly locked: readonly boolean[]
  /** Per-column "is mid scale-flash" flag — true for the
   *  `SLOT_LOCK_FLASH_MS` window after the column locked. The
   *  React leaf renders a transform on the column when this is
   *  true. */
  readonly flashing: readonly boolean[]
  /** Monotonically increasing tick count since `startSlotMachine`. */
  readonly tickMs: number
  /** Cycle-frame counter — used to pick the deterministic glyph
   *  index when the cycle phase is rendering. Bumped once per
   *  `tickSlotMachine` call inside the cycle phase. */
  readonly cycleFrame: number
}

/** Sentinel "no animation in progress" state. */
export const SLOT_IDLE_STATE: SlotMachineState = Object.freeze({
  phase: "idle",
  target: "",
  columns: Object.freeze([]),
  locked: Object.freeze([]),
  flashing: Object.freeze([]),
  tickMs: 0,
  cycleFrame: 0,
})

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Pure state-machine helpers
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Begin a new slot animation onto `target`. Returns the cycle-phase
 *  state. The caller drives the animation forward by calling
 *  `tickSlotMachine(state, deltaMs)` at ~24fps. */
export function startSlotMachine(target: string): SlotMachineState {
  const len = target.length
  const initialGlyphs: string[] = []
  for (let i = 0; i < len; i += 1) {
    initialGlyphs.push(_pickGlyph(0, i))
  }
  return Object.freeze({
    phase: "cycle",
    target,
    columns: Object.freeze(initialGlyphs),
    locked: Object.freeze(new Array<boolean>(len).fill(false)),
    flashing: Object.freeze(new Array<boolean>(len).fill(false)),
    tickMs: 0,
    cycleFrame: 0,
  })
}

/** Compute the total animation duration for a given column count. */
export function slotMachineDurationMs(targetLength: number): number {
  const animated = Math.min(targetLength, SLOT_MAX_ANIMATED_COLUMNS)
  if (animated <= 0) return 0
  const collapseTotal = (animated - 1) * SLOT_COLLAPSE_STAGGER_MS + SLOT_LOCK_FLASH_MS
  return SLOT_CYCLE_DURATION_MS + collapseTotal
}

/** Return the column index that should lock at `tickMs` into the
 *  collapse phase, or `-1` if no new column locks at this tick.
 *
 *  Phase boundary semantics:
 *    - tickMs < SLOT_CYCLE_DURATION_MS → cycle phase, returns -1
 *    - tickMs >= SLOT_CYCLE_DURATION_MS + N * SLOT_COLLAPSE_STAGGER_MS
 *      → column N locks (clamped to length-1)
 *
 *  Used by `tickSlotMachine` and pinned in tests so the boundary
 *  conditions are unambiguous. */
export function lockingColumnAt(
  tickMs: number,
  targetLength: number,
): number {
  if (targetLength <= 0) return -1
  const animated = Math.min(targetLength, SLOT_MAX_ANIMATED_COLUMNS)
  if (tickMs < SLOT_CYCLE_DURATION_MS) return -1
  const collapseElapsed = tickMs - SLOT_CYCLE_DURATION_MS
  const idx = Math.floor(collapseElapsed / SLOT_COLLAPSE_STAGGER_MS)
  if (idx < 0) return -1
  if (idx >= animated) return -1
  return idx
}

interface TickInput {
  readonly state: SlotMachineState
  readonly deltaMs: number
}

/** Advance the slot machine by `deltaMs`. Returns a new immutable
 *  state. This is the single reducer the React leaf calls inside
 *  its rAF tick. */
export function tickSlotMachine(input: TickInput): SlotMachineState {
  const { state, deltaMs } = input
  if (state.phase === "idle" || state.phase === "settled") return state
  if (deltaMs < 0) return state

  const newTickMs = state.tickMs + deltaMs
  const len = state.target.length
  const animated = Math.min(len, SLOT_MAX_ANIMATED_COLUMNS)

  // Snapshot the current (mutable inside this function only) arrays.
  const columns = state.columns.slice() as string[]
  const locked = state.locked.slice() as boolean[]
  const flashing = state.flashing.slice() as boolean[]

  // ─── Cycle phase ────────────────────────────────────────────
  let cycleFrame = state.cycleFrame
  if (newTickMs < SLOT_CYCLE_DURATION_MS) {
    cycleFrame += 1
    for (let i = 0; i < animated; i += 1) {
      if (!locked[i]) columns[i] = _pickGlyph(cycleFrame, i)
    }
    return Object.freeze({
      ...state,
      columns: Object.freeze(columns),
      locked: Object.freeze(locked),
      flashing: Object.freeze(flashing),
      tickMs: newTickMs,
      cycleFrame,
    })
  }

  // ─── Collapse phase ─────────────────────────────────────────
  // Lock every column whose stagger threshold has been crossed
  // since the previous tick. Multiple columns can lock in a single
  // tick if `deltaMs` is large (e.g. tab was backgrounded).
  const oldLockingIdx = lockingColumnAt(state.tickMs, len)
  const newLockingIdx = lockingColumnAt(newTickMs, len)

  // Tail columns past SLOT_MAX_ANIMATED_COLUMNS lock immediately
  // when collapse begins so the password isn't truncated visually.
  if (state.tickMs < SLOT_CYCLE_DURATION_MS && len > animated) {
    for (let i = animated; i < len; i += 1) {
      columns[i] = state.target.charAt(i)
      locked[i] = true
    }
  }

  // Compute the inclusive last animated column index this tick
  // should have locked. `lockingColumnAt` returns -1 when tickMs
  // has gone *past* the last animated column — in that case we
  // still need to flush every remaining animated column so the
  // animation can reach `settled` even when a single oversized
  // tick covers the whole timeline (e.g. tab unfreeze, fake-timer
  // jump).
  const collapseStarted = newTickMs >= SLOT_CYCLE_DURATION_MS
  const effectiveNewLocking =
    newLockingIdx >= 0
      ? newLockingIdx
      : collapseStarted
        ? animated - 1
        : -1

  // Lock every column from `oldLockingIdx + 1` through the
  // effective last-locking index for this tick.
  const startLock = Math.max(0, oldLockingIdx + 1)
  const endLock = effectiveNewLocking
  for (let i = startLock; i <= endLock; i += 1) {
    if (i >= len) break
    columns[i] = state.target.charAt(i)
    locked[i] = true
    flashing[i] = true
  }

  // Clear `flashing` for columns whose flash window has elapsed.
  for (let i = 0; i < animated; i += 1) {
    if (!locked[i]) continue
    const lockedAtMs =
      SLOT_CYCLE_DURATION_MS + i * SLOT_COLLAPSE_STAGGER_MS
    if (newTickMs - lockedAtMs >= SLOT_LOCK_FLASH_MS) {
      flashing[i] = false
    }
  }

  // Cycle-rotate any not-yet-locked column so the eye keeps moving
  // even after the collapse phase started.
  cycleFrame += 1
  for (let i = 0; i < animated; i += 1) {
    if (!locked[i]) columns[i] = _pickGlyph(cycleFrame, i)
  }

  // ─── Settled? ───────────────────────────────────────────────
  let allLocked = true
  for (let i = 0; i < len; i += 1) {
    if (!locked[i]) {
      allLocked = false
      break
    }
  }
  if (allLocked) {
    // Ensure no flash flag lingers when we land in settled.
    const flashingCleared = flashing.map(() => false)
    // Snap to target characters so any deterministic-glyph residue
    // is replaced with the real password chars.
    for (let i = 0; i < len; i += 1) columns[i] = state.target.charAt(i)
    return Object.freeze({
      ...state,
      phase: "settled",
      columns: Object.freeze(columns),
      locked: Object.freeze(locked),
      flashing: Object.freeze(flashingCleared),
      tickMs: newTickMs,
      cycleFrame,
    })
  }

  return Object.freeze({
    ...state,
    phase: "collapse",
    columns: Object.freeze(columns),
    locked: Object.freeze(locked),
    flashing: Object.freeze(flashing),
    tickMs: newTickMs,
    cycleFrame,
  })
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Internals — deterministic glyph picker
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Deterministic glyph index given the current cycle frame +
 *  column. Uses a small 32-bit Knuth-style hash so adjacent
 *  columns / frames land on different glyphs without
 *  `Math.random` (which would fail the SOP-§1 cross-tab
 *  determinism audit). Pinned by the test. */
export function _pickGlyph(frame: number, column: number): string {
  // 0x9E3779B9 is the golden-ratio multiplicative hash constant —
  // ubiquitous in PRNG-style scramblers. Combined with the column
  // index this gives a visually-random but reproducible glyph
  // sequence (`_pickGlyph(7, 3)` always returns the same char).
  const mixed = (frame * 0x9e3779b9 + column * 0x85ebca6b) >>> 0
  const idx = mixed % SLOT_CYCLE_GLYPHS.length
  return SLOT_CYCLE_GLYPHS.charAt(idx)
}
