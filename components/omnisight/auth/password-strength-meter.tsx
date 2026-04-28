"use client"

/**
 * AS.7.2 — Password strength + breach meter.
 *
 * Combines two signals:
 *
 *   1. **zxcvbn-aligned strength score** from `lib/password_strength
 *      .ts::estimatePasswordStrength` (0..4 scale, K7-unified gate
 *      requires ≥3 + ≥12 chars).
 *   2. **HIBP breach lookup** from `lib/auth/breach-check.ts::breachCount`
 *      (k-anonymity SHA-1 prefix lookup; client-side only).
 *
 * Visual: a 5-segment bar that lights up to the score, plus a small
 * status line below ("Strong" / "Weak — 12 chars min" / "Found in
 * 184k breaches" / "Network check skipped"). Updates in real time
 * as the user types or as the auto-generator settles a new value.
 *
 * Module-global state audit: leaf React state. The HIBP lookup is
 * debounced + cancellation-safe via AbortController; only the most
 * recent password value's lookup commits to state. No module-level
 * mutable container.
 *
 * Read-after-write timing audit: the breach check is async but the
 * AbortController + most-recent-wins pattern means only one result
 * ever lands per user typing burst — older lookups are aborted.
 */

import { useEffect, useRef, useState } from "react"

import {
  estimatePasswordStrength,
  type StrengthResult,
} from "@/lib/password_strength"
import {
  breachCount,
  type BreachResult,
} from "@/lib/auth/breach-check"

interface PasswordStrengthMeterProps {
  /** Live password value. */
  password: string
  /** Override the breach-check fetch impl — used by tests so they
   *  can stub HIBP responses without hitting the network. */
  fetchImpl?: typeof fetch
  /** Disable the breach lookup entirely (e.g. on a customer-private
   *  network where outbound HIBP is blocked). The meter still shows
   *  the rules-based strength score. Default false. */
  disableBreachCheck?: boolean
  /** Debounce window before firing the HIBP lookup (ms). The
   *  signup page passes 350 ms so a fast typer doesn't fan out N
   *  network calls; tests pass 0 to fire synchronously. */
  breachDebounceMs?: number
  /** Fires whenever the strength result changes so the parent can
   *  gate the submit button on `result.passes`. */
  onStrengthChange?: (result: StrengthResult) => void
}

const DEFAULT_DEBOUNCE_MS = 350

export function PasswordStrengthMeter({
  password,
  fetchImpl,
  disableBreachCheck = false,
  breachDebounceMs = DEFAULT_DEBOUNCE_MS,
  onStrengthChange,
}: PasswordStrengthMeterProps) {
  const strength = estimatePasswordStrength(password)

  const [breach, setBreach] = useState<BreachResult>(
    Object.freeze({ status: "skipped", count: null }),
  )

  // Notify the parent of strength changes without re-running the
  // hook on every render.
  const latestStrengthRef = useRef<StrengthResult | null>(null)
  useEffect(() => {
    if (latestStrengthRef.current?.passes !== strength.passes ||
        latestStrengthRef.current?.score !== strength.score) {
      latestStrengthRef.current = strength
      onStrengthChange?.(strength)
    }
  }, [strength, onStrengthChange])

  // Debounced HIBP lookup. AbortController is recreated per debounce
  // window so the previous fetch is dropped if the password changes
  // again before the timer fires. `disableBreachCheck` short-circuits
  // entirely.
  useEffect(() => {
    if (disableBreachCheck) {
      setBreach(Object.freeze({ status: "skipped", count: null }))
      return
    }
    if (!password) {
      setBreach(Object.freeze({ status: "skipped", count: null }))
      return
    }
    const controller = new AbortController()
    const timer = window.setTimeout(() => {
      void breachCount(password, {
        fetchImpl,
        signal: controller.signal,
      }).then((result) => {
        if (controller.signal.aborted) return
        setBreach(result)
      })
    }, breachDebounceMs)
    return () => {
      window.clearTimeout(timer)
      controller.abort()
    }
  }, [password, fetchImpl, disableBreachCheck, breachDebounceMs])

  const segments = [0, 1, 2, 3, 4]
  // 5 segments map to 0..4 score; rendered fully if score >= idx + 1.
  const fillUntil = strength.score
  const passes = strength.passes

  // Compose the status line. Breach trumps strength when present
  // because a breached password always disqualifies regardless of
  // entropy.
  let statusLine: string
  let statusKind: "ok" | "warn" | "bad" | "neutral"
  if (breach.status === "breached" && breach.count) {
    statusLine = `Found in ${breach.count.toLocaleString()} known breaches — pick another.`
    statusKind = "bad"
  } else if (!password) {
    statusLine = strength.hint
    statusKind = "neutral"
  } else if (!passes) {
    statusLine = strength.hint
    statusKind = "warn"
  } else {
    if (breach.status === "ok") {
      statusLine = `${capitalize(strength.label)} — never seen in any HIBP breach.`
    } else if (breach.status === "unknown") {
      statusLine = `${capitalize(strength.label)} — breach check unavailable (offline?).`
    } else {
      statusLine = strength.hint
    }
    statusKind = "ok"
  }

  return (
    <div
      data-testid="as7-password-strength-meter"
      data-as7-strength-passes={passes ? "yes" : "no"}
      data-as7-strength-label={strength.label}
      data-as7-breach-status={breach.status}
      className="as7-strength-meter flex flex-col gap-1"
    >
      <div
        role="meter"
        aria-label="Password strength"
        aria-valuemin={0}
        aria-valuemax={4}
        aria-valuenow={strength.score}
        className="flex items-center gap-1"
      >
        {segments.map((i) => {
          const filled = i < fillUntil + 1 && password.length > 0
          return (
            <span
              key={i}
              data-testid={`as7-strength-seg-${i}`}
              data-as7-strength-fill={filled ? "yes" : "no"}
              className={[
                "as7-strength-seg h-1.5 flex-1 rounded transition-colors",
                filled ? _segFillColor(strength.score) : "bg-[var(--border)]",
              ].join(" ")}
            />
          )
        })}
      </div>
      <p
        data-testid="as7-strength-status"
        data-as7-strength-kind={statusKind}
        className={[
          "font-mono text-[10px] leading-snug",
          _statusColor(statusKind),
        ].join(" ")}
      >
        {statusLine}
      </p>
    </div>
  )
}

function capitalize(s: string): string {
  if (!s) return s
  return s.charAt(0).toUpperCase() + s.slice(1)
}

function _segFillColor(score: number): string {
  if (score <= 1) return "bg-[var(--destructive)]"
  if (score === 2) return "bg-amber-500"
  if (score === 3) return "bg-emerald-500"
  return "bg-[var(--artifact-purple)]"
}

function _statusColor(kind: "ok" | "warn" | "bad" | "neutral"): string {
  if (kind === "bad") return "text-[var(--destructive)]"
  if (kind === "warn") return "text-amber-500"
  if (kind === "ok") return "text-emerald-500"
  return "text-[var(--muted-foreground)]"
}
