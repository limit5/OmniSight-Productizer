/**
 * Lightweight client-side password strength estimator.
 *
 * Approximates the zxcvbn 0–4 score used by backend
 * `auth.validate_password_strength` so wizard Step 1 can show live
 * feedback BEFORE submit. The server remains authoritative — the
 * K7-unified strength gate (12 chars + zxcvbn ≥ 3) re-runs in
 * `POST /api/v1/bootstrap/admin-password` with the real zxcvbn engine.
 *
 * We deliberately avoid bundling zxcvbn (~800KB) on the client; a
 * rules-based heuristic is enough for pre-submit UX.
 */
export const PASSWORD_MIN_LENGTH = 12
export const PASSWORD_MIN_SCORE = 3

const COMMON_BAD = [
  "password",
  "passw0rd",
  "qwerty",
  "letmein",
  "welcome",
  "admin",
  "omnisight",
  "iloveyou",
  "dragon",
  "monkey",
  "baseball",
  "football",
  "abc123",
  "111111",
  "123123",
  "1q2w3e",
]

const SEQUENCES = [
  "abcdefghijklmnopqrstuvwxyz",
  "zyxwvutsrqponmlkjihgfedcba",
  "0123456789",
  "9876543210",
  "qwertyuiop",
  "asdfghjkl",
  "zxcvbnm",
]

function hasSequence(pw: string): boolean {
  const lc = pw.toLowerCase()
  for (const seq of SEQUENCES) {
    for (let i = 0; i <= seq.length - 4; i++) {
      if (lc.includes(seq.slice(i, i + 4))) return true
    }
  }
  return false
}

function hasRepeat(pw: string): boolean {
  return /(.)\1{2,}/.test(pw)
}

function classCount(pw: string): number {
  let n = 0
  if (/[a-z]/.test(pw)) n++
  if (/[A-Z]/.test(pw)) n++
  if (/[0-9]/.test(pw)) n++
  if (/[^a-zA-Z0-9]/.test(pw)) n++
  return n
}

export interface StrengthResult {
  /** 0–4 score, zxcvbn-aligned scale. */
  score: number
  /** Human-readable band label. */
  label: "empty" | "very-weak" | "weak" | "fair" | "good" | "strong"
  /** Passes the K7-unified gate (≥ 12 chars AND score ≥ 3). */
  passes: boolean
  /** Actionable hint shown under the field. */
  hint: string
}

export function estimatePasswordStrength(pw: string): StrengthResult {
  if (!pw) {
    return {
      score: 0,
      label: "empty",
      passes: false,
      hint: "Enter a password (min 12 chars, score ≥ 3).",
    }
  }

  const lc = pw.toLowerCase()

  // Hard disqualifier: any common password substring → score 0.
  for (const bad of COMMON_BAD) {
    if (lc.includes(bad)) {
      return {
        score: 0,
        label: "very-weak",
        passes: false,
        hint: "Contains a common password — pick something less predictable.",
      }
    }
  }

  // Base score from length.
  let score = 0
  if (pw.length >= 8) score = 1
  if (pw.length >= 12) score = 2
  if (pw.length >= 16) score = 3
  if (pw.length >= 20) score = 4

  // Character class diversity bumps score.
  const classes = classCount(pw)
  if (classes >= 3 && pw.length >= 12) score = Math.max(score, 3)
  if (classes === 4 && pw.length >= 14) score = Math.max(score, 4)

  // Penalties.
  if (hasRepeat(pw)) score = Math.max(0, score - 1)
  if (hasSequence(pw)) score = Math.max(0, score - 1)
  if (classes === 1) score = Math.min(score, 1)
  if (classes === 2 && pw.length < 16) score = Math.min(score, 2)

  // Length floor — the K7 minimum.
  if (pw.length < PASSWORD_MIN_LENGTH) {
    score = Math.min(score, 1)
  }

  const passes = pw.length >= PASSWORD_MIN_LENGTH && score >= PASSWORD_MIN_SCORE

  const labels: StrengthResult["label"][] = [
    "very-weak",
    "weak",
    "fair",
    "good",
    "strong",
  ]
  const label = labels[Math.max(0, Math.min(4, score))]

  let hint: string
  if (pw.length < PASSWORD_MIN_LENGTH) {
    hint = `Needs at least ${PASSWORD_MIN_LENGTH} characters (currently ${pw.length}).`
  } else if (score < PASSWORD_MIN_SCORE) {
    hint =
      "Too guessable — try mixing upper/lowercase, digits, and symbols, " +
      "and avoid dictionary words."
  } else if (score === 3) {
    hint = "Meets the requirement. Add more length for stronger protection."
  } else {
    hint = "Strong password."
  }

  return { score, label, passes, hint }
}
