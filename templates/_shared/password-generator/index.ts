/**
 * AS.0.10 — Auto-generated password core library (TypeScript twin).
 *
 * Behaviourally identical mirror of `backend/auth/password_generator.py`.
 *
 * Three styles per `docs/design/as-auth-security-shared-library.md` §4:
 *
 *   Style A — Random          alphanumeric + symbols (default 20 chars)
 *   Style B — Diceware        word1-word2-word3-word4-DD (memorable)
 *   Style C — Pronounceable   syllable1-syllable2-DD (consonant-vowel)
 *
 * Randomness comes from `crypto.getRandomValues` (Web Crypto API). The
 * module is pure-functional and side-effect free — no module-level
 * mutable state, no caches, no IO.
 *
 * AS.0.8 §3.1 noop matrix: this module remains importable when
 * `OMNISIGHT_AS_FRONTEND_ENABLED=false`; only the slot-machine UI in
 * the AS.7.2 signup page wires it up. The lib itself never reads any
 * env knob.
 *
 * Cross-twin parity: the wordlist + symbol pool + consonant / vowel
 * pools must hash-match `backend/auth/password_generator.py`. The
 * Python-side test
 * `backend/tests/test_password_generator.py::test_wordlist_parity_python_ts`
 * loads this file, parses the constants out by regex, hashes them,
 * and asserts SHA-256 equality with the Python tuple. Divergence
 * breaks CI.
 */

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Constants — must mirror Python side
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Symbol pool used by Style A. Curated for password-manager / form input
 * compatibility (no quotes, no backslash, no backtick). */
export const SYMBOL_POOL = "!@#$%^&*()-_=+[]{};:,.<>?"

/** Visually ambiguous characters excluded when `excludeAmbiguous=true`. */
export const AMBIGUOUS_CHARS = "0Ol1I"

/** Allowed separators for Style B / Style C. */
export const ALLOWED_SEPARATORS = ["-", "_", ".", " "] as const
export type Separator = (typeof ALLOWED_SEPARATORS)[number]

/** Style C consonants (q dropped — awkward; x dropped — rare initial). */
export const CONSONANTS = "bcdfghjklmnpqrstvwz"
/** Style C vowels (no y to keep CV strictly distinct). */
export const VOWELS = "aeiou"

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Diceware wordlist — 256 entries (8 bits / word)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//
// CRITICAL: order, casing, and content are part of the cross-twin
// contract. Any change MUST be mirrored in the Python side
// (`backend/auth/password_generator.py::DICEWARE_WORDLIST`). The
// drift-guard test enforces SHA-256 equality.

export const DICEWARE_WORDLIST: readonly string[] = [
  "able", "acid", "aged", "airy", "ajar", "akin", "amid", "amok",
  "ankle", "apex", "arch", "arena", "army", "atlas", "atom", "aunt",
  "auto", "axis", "back", "bake", "bald", "ball", "band", "bank",
  "bare", "bark", "barn", "base", "bass", "bath", "beach", "bead",
  "beam", "bean", "bear", "beef", "bell", "belt", "bench", "bend",
  "best", "bike", "bill", "bind", "bird", "black", "blaze", "blend",
  "blink", "block", "blue", "boat", "body", "bold", "bond", "bone",
  "book", "boom", "boot", "born", "boss", "both", "bowl", "brace",
  "brain", "brake", "brave", "bread", "brick", "brief", "broad", "brook",
  "broom", "brown", "build", "bulk", "bull", "burn", "bush", "cabin",
  "cable", "cake", "calf", "calm", "camel", "camp", "canal", "candy",
  "cane", "cape", "card", "care", "cargo", "carve", "case", "cash",
  "cast", "cave", "chain", "chair", "chalk", "champ", "chart", "cheek",
  "cheer", "chef", "chess", "chest", "chick", "chief", "child", "chin",
  "chip", "chord", "chunk", "civic", "claim", "clam", "clamp", "clan",
  "clap", "clash", "clasp", "class", "clean", "clear", "clerk", "click",
  "cliff", "climb", "cling", "clip", "clock", "cloth", "cloud", "clove",
  "clown", "club", "clue", "coach", "coal", "coast", "coat", "code",
  "coil", "coin", "cold", "color", "colt", "comb", "come", "cone",
  "cook", "cool", "copy", "coral", "cord", "core", "cork", "corn",
  "couch", "count", "court", "cove", "cover", "cow", "crab", "craft",
  "cramp", "crane", "crash", "crate", "crawl", "cream", "creek", "crepe",
  "crest", "crew", "crib", "crisp", "crop", "cross", "crow", "crowd",
  "crown", "crude", "cruel", "crumb", "crunch", "crust", "cube", "cuff",
  "curb", "curl", "curse", "curve", "cycle", "daily", "dairy", "dance",
  "dandy", "dark", "dart", "data", "dawn", "deal", "dean", "debt",
  "deck", "deed", "deep", "deer", "delta", "den", "dent", "depth",
  "desk", "diary", "dice", "diet", "dig", "dim", "dime", "diner",
  "dip", "dirt", "dish", "ditch", "dive", "dock", "doe", "dog",
  "doll", "dome", "door", "dose", "dot", "double", "dough", "dove",
  "dozen", "draft", "drag", "drain", "drama", "draw", "dream", "dress",
  "drift", "drill", "drink", "drive", "drop", "drum", "duck", "dune",
]

if (DICEWARE_WORDLIST.length !== 256) {
  throw new Error(
    `DICEWARE_WORDLIST must have exactly 256 entries, got ${DICEWARE_WORDLIST.length}`,
  )
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Public types
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export type PasswordStyle = "random" | "diceware" | "pronounceable"

export interface GeneratedPassword {
  /** The generated password string. */
  readonly password: string
  /** Style identifier. */
  readonly style: PasswordStyle
  /** Approximate entropy in bits, rounded to one decimal. */
  readonly entropyBits: number
  /** `password.length` — convenience field. */
  readonly length: number
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Cryptographic random helpers
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Resolve the platform Web-Crypto `crypto.getRandomValues` impl, throwing
 * a typed error if no secure source is available (e.g. a server-side
 * runtime without globalThis.crypto). */
function getCrypto(): Crypto {
  // Node 19+ exposes globalThis.crypto; browsers always do.
  const c = (globalThis as { crypto?: Crypto }).crypto
  if (!c || typeof c.getRandomValues !== "function") {
    throw new Error(
      "Web Crypto API not available — secure random source is required for password generation",
    )
  }
  return c
}

/** Uniform-random integer in `[0, max)` via rejection sampling on
 * 32-bit unsigned ints. Matches Python `secrets.randbelow` semantics. */
function randBelow(max: number): number {
  if (!Number.isInteger(max) || max <= 0 || max > 0x100000000) {
    throw new Error(`randBelow: invalid max=${max}`)
  }
  const c = getCrypto()
  const buf = new Uint32Array(1)
  // Largest multiple of `max` that fits in 2^32 — anything above is
  // rejected to keep the distribution exactly uniform.
  const limit = Math.floor(0x100000000 / max) * max
  // eslint-disable-next-line no-constant-condition
  while (true) {
    c.getRandomValues(buf)
    if (buf[0] < limit) return buf[0] % max
  }
}

/** Uniform-random pick from a string. */
function pickChar(pool: string): string {
  return pool.charAt(randBelow(pool.length))
}

/** Uniform-random pick from a readonly array. */
function pickWord(words: readonly string[]): string {
  return words[randBelow(words.length)]
}

/** Cryptographic Fisher–Yates shuffle (in-place, returns same array). */
function shuffle<T>(items: T[]): T[] {
  for (let i = items.length - 1; i > 0; i--) {
    const j = randBelow(i + 1)
    ;[items[i], items[j]] = [items[j], items[i]]
  }
  return items
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Style A — Random
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export const RANDOM_DEFAULT_LENGTH = 20
export const RANDOM_MIN_LENGTH = 8
export const RANDOM_MAX_LENGTH = 128

export interface GenerateRandomOptions {
  /** Total length (default 20). Range [8, 128]. */
  length?: number
  /** Include the SYMBOL_POOL characters. Default true. */
  useSymbols?: boolean
  /** Drop visually-confusable characters (`0 O l 1 I`). Default false. */
  excludeAmbiguous?: boolean
  /** Enforce ≥1 char per non-empty class. Default true. */
  requireClasses?: boolean
}

function buildRandomPool(
  useSymbols: boolean,
  excludeAmbiguous: boolean,
): { lower: string; upper: string; digits: string; symbols: string } {
  let lower = "abcdefghijklmnopqrstuvwxyz"
  let upper = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
  let digits = "0123456789"
  const symbols = useSymbols ? SYMBOL_POOL : ""
  if (excludeAmbiguous) {
    const ambig = new Set(AMBIGUOUS_CHARS.split(""))
    lower = [...lower].filter((ch) => !ambig.has(ch)).join("")
    upper = [...upper].filter((ch) => !ambig.has(ch)).join("")
    digits = [...digits].filter((ch) => !ambig.has(ch)).join("")
  }
  return { lower, upper, digits, symbols }
}

export function generateRandom(opts: GenerateRandomOptions = {}): GeneratedPassword {
  const length = opts.length ?? RANDOM_DEFAULT_LENGTH
  const useSymbols = opts.useSymbols ?? true
  const excludeAmbiguous = opts.excludeAmbiguous ?? false
  const requireClasses = opts.requireClasses ?? true

  if (length < RANDOM_MIN_LENGTH || length > RANDOM_MAX_LENGTH) {
    throw new RangeError(
      `length must be in [${RANDOM_MIN_LENGTH}, ${RANDOM_MAX_LENGTH}], got ${length}`,
    )
  }

  const { lower, upper, digits, symbols } = buildRandomPool(useSymbols, excludeAmbiguous)
  const classes = [lower, upper, digits, symbols].filter((c) => c.length > 0)
  if (classes.length === 0) {
    throw new Error("character pool is empty after applying filters")
  }
  const fullPool = classes.join("")

  let chars: string[]
  if (requireClasses) {
    if (length < classes.length) {
      throw new RangeError(
        `length ${length} cannot satisfy ${classes.length} required classes`,
      )
    }
    const seeded = classes.map((c) => pickChar(c))
    const remaining = length - seeded.length
    for (let i = 0; i < remaining; i++) seeded.push(pickChar(fullPool))
    chars = shuffle(seeded)
  } else {
    chars = []
    for (let i = 0; i < length; i++) chars.push(pickChar(fullPool))
  }

  const password = chars.join("")
  const entropy = entropyUniform(fullPool.length, length)
  return {
    password,
    style: "random",
    entropyBits: round1(entropy),
    length: password.length,
  }
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Style B — Diceware
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export const DICEWARE_DEFAULT_WORDS = 4
export const DICEWARE_MIN_WORDS = 3
export const DICEWARE_MAX_WORDS = 12

export interface GenerateDicewareOptions {
  /** Number of words (default 4). Range [3, 12]. */
  numWords?: number
  /** Separator. Default "-". Must be in ALLOWED_SEPARATORS. */
  separator?: Separator
  /** Trailing decimal digits (default 2). Range [0, 6]. */
  appendDigits?: number
  /** Capitalize first letter of each word. Default false. */
  capitalize?: boolean
}

export function generateDiceware(opts: GenerateDicewareOptions = {}): GeneratedPassword {
  const numWords = opts.numWords ?? DICEWARE_DEFAULT_WORDS
  const separator = opts.separator ?? "-"
  const appendDigits = opts.appendDigits ?? 2
  const capitalize = opts.capitalize ?? false

  if (numWords < DICEWARE_MIN_WORDS || numWords > DICEWARE_MAX_WORDS) {
    throw new RangeError(
      `numWords must be in [${DICEWARE_MIN_WORDS}, ${DICEWARE_MAX_WORDS}], got ${numWords}`,
    )
  }
  if (!ALLOWED_SEPARATORS.includes(separator)) {
    throw new RangeError(`separator must be one of ${JSON.stringify(ALLOWED_SEPARATORS)}, got ${JSON.stringify(separator)}`)
  }
  if (appendDigits < 0 || appendDigits > 6) {
    throw new RangeError(`appendDigits must be in [0, 6], got ${appendDigits}`)
  }

  const words: string[] = []
  for (let i = 0; i < numWords; i++) {
    const w = pickWord(DICEWARE_WORDLIST)
    words.push(capitalize ? w.charAt(0).toUpperCase() + w.slice(1) : w)
  }
  const parts = [...words]
  if (appendDigits > 0) {
    let dig = ""
    for (let i = 0; i < appendDigits; i++) dig += String(randBelow(10))
    parts.push(dig)
  }
  const password = parts.join(separator)

  const wordBits = entropyUniform(DICEWARE_WORDLIST.length, numWords)
  const digitBits = appendDigits > 0 ? entropyUniform(10, appendDigits) : 0
  return {
    password,
    style: "diceware",
    entropyBits: round1(wordBits + digitBits),
    length: password.length,
  }
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Style C — Pronounceable
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export const PRONOUNCEABLE_DEFAULT_SYLLABLES = 3
export const PRONOUNCEABLE_MIN_SYLLABLES = 2
export const PRONOUNCEABLE_MAX_SYLLABLES = 8

export const PRONOUNCEABLE_DEFAULT_PAIRS_PER_SYLLABLE = 3
export const PRONOUNCEABLE_MIN_PAIRS_PER_SYLLABLE = 2
export const PRONOUNCEABLE_MAX_PAIRS_PER_SYLLABLE = 5

export interface GeneratePronounceableOptions {
  /** Number of syllables (default 3). Range [2, 8]. */
  numSyllables?: number
  /** CV pairs per syllable (default 3 → 6 chars). Range [2, 5]. */
  pairsPerSyllable?: number
  /** Separator. Default "-". */
  separator?: Separator
  /** Trailing decimal digits (default 2). Range [0, 6]. */
  appendDigits?: number
}

function makeSyllable(pairs: number): string {
  let out = ""
  for (let i = 0; i < pairs; i++) {
    out += pickChar(CONSONANTS)
    out += pickChar(VOWELS)
  }
  return out
}

export function generatePronounceable(
  opts: GeneratePronounceableOptions = {},
): GeneratedPassword {
  const numSyllables = opts.numSyllables ?? PRONOUNCEABLE_DEFAULT_SYLLABLES
  const pairsPerSyllable = opts.pairsPerSyllable ?? PRONOUNCEABLE_DEFAULT_PAIRS_PER_SYLLABLE
  const separator = opts.separator ?? "-"
  const appendDigits = opts.appendDigits ?? 2

  if (
    numSyllables < PRONOUNCEABLE_MIN_SYLLABLES ||
    numSyllables > PRONOUNCEABLE_MAX_SYLLABLES
  ) {
    throw new RangeError(
      `numSyllables must be in [${PRONOUNCEABLE_MIN_SYLLABLES}, ${PRONOUNCEABLE_MAX_SYLLABLES}], got ${numSyllables}`,
    )
  }
  if (
    pairsPerSyllable < PRONOUNCEABLE_MIN_PAIRS_PER_SYLLABLE ||
    pairsPerSyllable > PRONOUNCEABLE_MAX_PAIRS_PER_SYLLABLE
  ) {
    throw new RangeError(
      `pairsPerSyllable must be in [${PRONOUNCEABLE_MIN_PAIRS_PER_SYLLABLE}, ${PRONOUNCEABLE_MAX_PAIRS_PER_SYLLABLE}], got ${pairsPerSyllable}`,
    )
  }
  if (!ALLOWED_SEPARATORS.includes(separator)) {
    throw new RangeError(`separator must be one of ${JSON.stringify(ALLOWED_SEPARATORS)}, got ${JSON.stringify(separator)}`)
  }
  if (appendDigits < 0 || appendDigits > 6) {
    throw new RangeError(`appendDigits must be in [0, 6], got ${appendDigits}`)
  }

  const syllables: string[] = []
  for (let i = 0; i < numSyllables; i++) syllables.push(makeSyllable(pairsPerSyllable))
  const parts = [...syllables]
  if (appendDigits > 0) {
    let dig = ""
    for (let i = 0; i < appendDigits; i++) dig += String(randBelow(10))
    parts.push(dig)
  }
  const password = parts.join(separator)

  const pairSpace = CONSONANTS.length * VOWELS.length
  const syllableBits = entropyUniform(pairSpace, pairsPerSyllable)
  const totalSyllableBits = syllableBits * numSyllables
  const digitBits = appendDigits > 0 ? entropyUniform(10, appendDigits) : 0
  return {
    password,
    style: "pronounceable",
    entropyBits: round1(totalSyllableBits + digitBits),
    length: password.length,
  }
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Single-style dispatcher
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export const VALID_STYLES: readonly PasswordStyle[] = [
  "random",
  "diceware",
  "pronounceable",
]

export interface GenerateOptions {
  random?: GenerateRandomOptions
  diceware?: GenerateDicewareOptions
  pronounceable?: GeneratePronounceableOptions
}

/** Convenience dispatcher mirroring Python `generate(style, **kwargs)`. */
export function generate(
  style: PasswordStyle,
  opts?: GenerateOptions,
): GeneratedPassword {
  switch (style) {
    case "random":
      return generateRandom(opts?.random)
    case "diceware":
      return generateDiceware(opts?.diceware)
    case "pronounceable":
      return generatePronounceable(opts?.pronounceable)
    default: {
      const _exhaustive: never = style
      throw new Error(`unknown style ${JSON.stringify(_exhaustive)}; expected one of ${JSON.stringify(VALID_STYLES)}`)
    }
  }
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Shared helpers
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

function entropyUniform(alphabetSize: number, length: number): number {
  if (alphabetSize <= 1 || length <= 0) return 0
  return Math.log2(alphabetSize) * length
}

function round1(n: number): number {
  return Math.round(n * 10) / 10
}
