"use client"

/**
 * AS.7.3 — Reset password page (stage 2/2 of password reset).
 *
 * Composes the AS.7.0 visual foundation + AS.7.1 login primitives +
 * AS.7.2 signup primitives (`<PasswordSlotMachine>` /
 * `<PasswordStyleToggle>` / `<PasswordStrengthMeter>` /
 * `<SaveAcknowledgementCheckbox>`) to deliver the new-password
 * stage of the reset flow. The user lands here from the magic link
 * with `?token=...` in the URL; the form posts the (token, new
 * password) pair to `/api/v1/auth/password-reset/confirm`.
 *
 * UX features (per AS.7.3 TODO row):
 *   - Auto-generate strong password on mount via the AS.0.10
 *     password generator (same library the signup page uses) with a
 *     style toggle (random / diceware / pronounceable) so the user
 *     can pick a memorable form. Mirrors AS.7.2 signup.
 *   - 🎲 re-roll button + 👁 SHOW/HIDE + Copy button.
 *   - "I have saved this password" gate — submit disabled until
 *     ack'd. Resetting the password rotates the saved value, so the
 *     user MUST take the moment to write the new password down.
 *   - Real-time strength meter + HIBP k-anonymity breach check
 *     reusing the AS.7.2 component.
 *   - Token-missing branch — when the URL has no `?token=` we land
 *     on a "this link is missing the token" terminal card instead
 *     of showing a form the user can never submit.
 *   - Error branches: invalid_token / expired_token / weak_password
 *     drive different copy + a "request a fresh link" CTA on the
 *     two token-failure branches.
 *
 * Module-global state audit (per implement_phase_step.md SOP §1):
 * pure browser component. All state lives in React (`useState` /
 * `useRef`). Per-tab / per-worker derivation is trivially identical
 * (Answer #1 of the SOP audit).
 *
 * Read-after-write timing audit: N/A — no DB, no parallelisation
 * change vs. existing auth-context behaviour.
 */

import { useEffect, useRef, useState } from "react"
import { useRouter, useSearchParams } from "next/navigation"
import {
  AlertCircle,
  ArrowLeft,
  CheckCircle2,
  Copy,
  Dice5,
  KeyRound,
  Loader2,
  Lock,
  Sparkles,
} from "lucide-react"

import { useAuth } from "@/lib/auth-context"
import { useEffectiveMotionLevel } from "@/hooks/use-effective-motion-level"
import {
  AuthBrandWordmark,
  AuthFieldElectric,
  AuthGlassCard,
  AuthHoneypotField,
  AuthTurnstileWidget,
  AuthVisualFoundation,
  PasswordSlotMachine,
  PasswordStrengthMeter,
  PasswordStyleToggle,
  SaveAcknowledgementCheckbox,
} from "@/components/omnisight/auth"
import { bumpShakeKey } from "@/lib/auth/login-form-helpers"
import {
  passwordResetHoneypotFieldName,
  resetPasswordSubmitBlockedReason,
} from "@/lib/auth/password-reset-helpers"
import {
  generate,
  type GeneratedPassword,
  type PasswordStyle,
} from "@/templates/_shared/password-generator"
import type { StrengthResult } from "@/lib/password_strength"

const TURNSTILE_SITE_KEY: string | null =
  process.env.NEXT_PUBLIC_TURNSTILE_SITE_KEY ?? null

// ─────────────────────────────────────────────────────────────────
// Token-missing terminal card.
// ─────────────────────────────────────────────────────────────────

function TokenMissingCard() {
  return (
    <div
      data-testid="as7-reset-token-missing"
      className="flex flex-col gap-4 items-center text-center"
    >
      <AlertCircle size={28} className="text-[var(--destructive)]" />
      <h1 className="font-mono text-base font-semibold text-[var(--foreground)]">
        Reset link missing token
      </h1>
      <p className="font-mono text-[12px] text-[var(--muted-foreground)] leading-relaxed">
        This link doesn&apos;t look right — there&apos;s no reset token
        in the URL. The link from your email may have been truncated
        when it was copied. Request a fresh link from the sign-in page.
      </p>
      <a
        href="/forgot-password"
        data-testid="as7-reset-request-fresh-link"
        className="flex items-center gap-2 px-3 py-2 rounded bg-[var(--artifact-purple)] text-white font-mono text-xs font-semibold hover:opacity-90"
      >
        Request a fresh link
      </a>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// Token-invalid / expired terminal card (failure branch).
// ─────────────────────────────────────────────────────────────────

function TokenFailureCard({
  message,
}: {
  message: string
}) {
  return (
    <div
      data-testid="as7-reset-token-failure"
      className="flex flex-col gap-4 items-center text-center"
    >
      <AlertCircle size={28} className="text-[var(--destructive)]" />
      <h1 className="font-mono text-base font-semibold text-[var(--foreground)]">
        This reset link is no longer valid
      </h1>
      <p className="font-mono text-[12px] text-[var(--muted-foreground)] leading-relaxed">
        {message}
      </p>
      <a
        href="/forgot-password"
        data-testid="as7-reset-request-fresh-link"
        className="flex items-center gap-2 px-3 py-2 rounded bg-[var(--artifact-purple)] text-white font-mono text-xs font-semibold hover:opacity-90"
      >
        Request a fresh link
      </a>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// Success terminal card.
// ─────────────────────────────────────────────────────────────────

function ResetSuccessCard({ email }: { email: string | null }) {
  return (
    <div
      data-testid="as7-reset-success"
      className="flex flex-col gap-4 items-center text-center"
    >
      <CheckCircle2 size={32} className="text-emerald-500" />
      <h1 className="font-mono text-base font-semibold text-[var(--foreground)]">
        Password updated
      </h1>
      <p className="font-mono text-[12px] text-[var(--muted-foreground)] leading-relaxed">
        {email ? (
          <>
            Your password for{" "}
            <span className="text-[var(--foreground)]">{email}</span>{" "}
            was updated successfully. You can now sign in with your new
            password.
          </>
        ) : (
          <>
            Your password was updated successfully. You can now sign in
            with your new password.
          </>
        )}
      </p>
      <a
        href="/login"
        data-testid="as7-reset-go-login"
        className="flex items-center gap-2 px-3 py-2 rounded bg-[var(--artifact-purple)] text-white font-mono text-xs font-semibold hover:opacity-90"
      >
        Sign in now
      </a>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// New-password form (default path).
// ─────────────────────────────────────────────────────────────────

function ResetPasswordForm({ token }: { token: string }) {
  const router = useRouter()
  const search = useSearchParams()
  const auth = useAuth()
  const level = useEffectiveMotionLevel()
  const next = search.get("next") || "/"

  const [password, setPassword] = useState("")
  const [style, setStyle] = useState<PasswordStyle>("random")
  const [generated, setGenerated] = useState<GeneratedPassword | null>(null)
  const [animationKey, setAnimationKey] = useState(0)
  const [hasSaved, setHasSaved] = useState(false)
  const [busy, setBusy] = useState(false)
  const [errorKey, setErrorKey] = useState(0)
  const [bloomKey, setBloomKey] = useState(0)
  const [turnstileToken, setTurnstileToken] = useState<string | null>(null)
  const [strength, setStrength] = useState<StrengthResult | null>(null)
  const [showPassword, setShowPassword] = useState(false)
  const [copied, setCopied] = useState(false)
  const [resetEmail, setResetEmail] = useState<string | null>(null)
  const [completed, setCompleted] = useState<boolean>(false)
  const honeypotFieldRef = useRef<string | null>(null)
  const honeypotResolvedRef = useRef<boolean>(false)
  const [, setHoneypotTick] = useState(0)
  const honeypotResolved = honeypotResolvedRef.current

  // If the user is already signed in, send them home — they can
  // change their password from /settings, not via this magic-link
  // flow.
  useEffect(() => {
    if (!auth.loading && auth.user && !completed) {
      router.replace(next)
    }
  }, [auth.loading, auth.user, next, router, completed])

  // Pre-fetch the rotating honeypot field name.
  useEffect(() => {
    let cancelled = false
    void passwordResetHoneypotFieldName()
      .then((name) => {
        if (cancelled) return
        honeypotFieldRef.current = name
        honeypotResolvedRef.current = true
        setHoneypotTick((t) => t + 1)
      })
      .catch(() => {
        // Web Crypto unavailable — gate stays closed.
      })
    return () => {
      cancelled = true
    }
  }, [])

  // Auto-roll a strong password on mount.
  const rollPassword = (nextStyle: PasswordStyle = style) => {
    const out = generate(nextStyle)
    setGenerated(out)
    setPassword(out.password)
    setHasSaved(false)
    setAnimationKey((k) => k + 1)
    setCopied(false)
  }

  useEffect(() => {
    rollPassword("random")
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const onStyleChange = (nextStyle: PasswordStyle) => {
    setStyle(nextStyle)
    rollPassword(nextStyle)
  }

  const onCopyPassword = async () => {
    if (!password) return
    try {
      await navigator.clipboard.writeText(password)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 2000)
    } catch {
      // Permission denied — silent. The user can still select-copy.
    }
  }

  const onFieldFocus = () => setBloomKey((k) => k + 1)

  const submitBlockedReason = resetPasswordSubmitBlockedReason({
    token,
    password,
    passwordPasses: strength?.passes ?? false,
    hasSaved,
    busy,
    honeypotResolved,
  })

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (busy || submitBlockedReason !== null) return
    setBusy(true)
    try {
      const extras: Record<string, string> = {}
      if (turnstileToken) extras.turnstile_token = turnstileToken
      if (honeypotFieldRef.current) {
        extras[honeypotFieldRef.current] = ""
      }
      const outcome = await auth.resetPassword(
        { token, password },
        extras,
      )
      if (outcome.status === "ok") {
        setResetEmail(outcome.email)
        setCompleted(true)
      } else {
        setErrorKey(bumpShakeKey)
      }
    } finally {
      setBusy(false)
    }
  }

  const errorOutcome = auth.lastResetPasswordError
  const hasError = Boolean(errorOutcome)
  const submitDisabled = submitBlockedReason !== null

  if (completed) {
    return <ResetSuccessCard email={resetEmail} />
  }

  // Token-failure branches go to a dedicated card so the user can
  // immediately request a new link. Weak-password / rate-limit /
  // service-unavailable stay inline so the user can retry.
  if (
    errorOutcome &&
    (errorOutcome.kind === "invalid_token" ||
      errorOutcome.kind === "expired_token")
  ) {
    return <TokenFailureCard message={errorOutcome.message} />
  }

  return (
    <form
      onSubmit={onSubmit}
      data-testid="as7-reset-form"
      className="flex flex-col gap-4 relative"
    >
      <div className="flex flex-col items-center gap-1.5 text-center">
        <AuthBrandWordmark level={level} bloomKey={bloomKey} />
        <KeyRound size={22} className="text-[var(--artifact-purple)]" />
        <h1 className="font-mono text-base font-semibold text-[var(--foreground)]">
          Set a new password
        </h1>
        <p className="font-mono text-[11px] text-[var(--muted-foreground)] leading-relaxed">
          Pick a strong password — we&apos;ve auto-generated one for
          you. Resetting will sign out every active session on your
          account.
        </p>
      </div>

      {/* Password slot machine block — same shape as AS.7.2 signup. */}
      <div
        data-testid="as7-reset-password-block"
        className="flex flex-col gap-2 rounded border border-[var(--border)] bg-[var(--background)]/30 p-3"
      >
        <div className="flex items-center justify-between gap-2">
          <span className="font-mono text-[10px] tracking-wider text-[var(--muted-foreground)] flex items-center gap-1">
            <Sparkles size={10} className="text-[var(--artifact-purple)]" />
            NEW PASSWORD
          </span>
          <div className="flex items-center gap-1">
            <button
              type="button"
              data-testid="as7-reset-reroll"
              onClick={() => rollPassword(style)}
              title="Roll a fresh password"
              aria-label="Roll a fresh password"
              className="p-1 rounded text-[var(--muted-foreground)] hover:text-[var(--foreground)] hover:bg-[var(--background)]/60"
            >
              <Dice5 size={14} />
            </button>
            <button
              type="button"
              data-testid="as7-reset-copy"
              onClick={onCopyPassword}
              title={copied ? "Copied!" : "Copy to clipboard"}
              aria-label="Copy password to clipboard"
              className="p-1 rounded text-[var(--muted-foreground)] hover:text-[var(--foreground)] hover:bg-[var(--background)]/60"
            >
              {copied ? (
                <CheckCircle2 size={14} className="text-emerald-500" />
              ) : (
                <Copy size={14} />
              )}
            </button>
            <button
              type="button"
              data-testid="as7-reset-toggle-show"
              onClick={() => setShowPassword((v) => !v)}
              aria-label={showPassword ? "Hide password" : "Show password"}
              aria-pressed={showPassword}
              className="font-mono text-[10px] px-2 py-0.5 rounded text-[var(--muted-foreground)] hover:text-[var(--foreground)] hover:bg-[var(--background)]/60"
            >
              {showPassword ? "HIDE" : "SHOW"}
            </button>
          </div>
        </div>

        {showPassword ? (
          <PasswordSlotMachine
            level={level}
            target={password}
            animationKey={animationKey}
          />
        ) : (
          <div
            data-testid="as7-reset-password-mask"
            className="font-mono text-sm tracking-widest text-[var(--foreground)] py-1"
          >
            {password ? "•".repeat(Math.min(password.length, 32)) : ""}
          </div>
        )}

        <PasswordStyleToggle value={style} onChange={onStyleChange} />

        <AuthFieldElectric
          level={level}
          label="PASSWORD"
          leadingIcon={<Lock size={14} />}
          hasError={hasError}
          errorKey={errorKey}
          inputProps={{
            name: "password",
            type: showPassword ? "text" : "password",
            autoComplete: "new-password",
            required: true,
            minLength: 12,
            value: password,
            onChange: (e) => setPassword(e.target.value),
            onFocus: onFieldFocus,
          }}
        />

        <PasswordStrengthMeter
          password={password}
          onStrengthChange={setStrength}
        />
      </div>

      <SaveAcknowledgementCheckbox
        checked={hasSaved}
        onChange={setHasSaved}
        disabled={!password || !(strength?.passes ?? false)}
      />

      <AuthHoneypotField
        onResolved={(name) => {
          honeypotFieldRef.current = name
          honeypotResolvedRef.current = true
          setHoneypotTick((t) => t + 1)
        }}
      />

      {TURNSTILE_SITE_KEY ? (
        <div className="flex justify-center">
          <AuthTurnstileWidget
            siteKey={TURNSTILE_SITE_KEY}
            action="password_reset_confirm"
            onToken={(token) => setTurnstileToken(token)}
            onExpired={() => setTurnstileToken(null)}
            onError={() => setTurnstileToken(null)}
          />
        </div>
      ) : null}

      {hasError && errorOutcome ? (
        <div
          role="alert"
          data-testid="as7-reset-error"
          className="flex items-start gap-2 p-2 rounded border border-[var(--destructive)] bg-[var(--destructive)]/10 text-[var(--destructive)] font-mono text-xs"
        >
          <AlertCircle size={14} className="shrink-0 mt-0.5" />
          <span>{errorOutcome.message}</span>
        </div>
      ) : null}

      <button
        type="submit"
        data-testid="as7-reset-submit"
        disabled={submitDisabled}
        data-as7-block-reason={submitBlockedReason ?? "ok"}
        className="flex items-center justify-center gap-2 px-3 py-2 rounded bg-[var(--artifact-purple)] text-white font-mono text-sm font-semibold hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed"
      >
        {busy ? <Loader2 size={14} className="animate-spin" /> : null}
        Update password
      </button>

      <p className="font-mono text-[10px] text-[var(--muted-foreground)] text-center leading-relaxed">
        Remembered the old one?{" "}
        <a
          href="/login"
          data-testid="as7-reset-back-link"
          className="underline hover:text-[var(--foreground)] inline-flex items-center gap-1"
        >
          <ArrowLeft size={10} /> Back to sign in
        </a>
      </p>

      {generated ? (
        <p
          data-testid="as7-reset-entropy-hint"
          className="font-mono text-[10px] text-[var(--muted-foreground)] text-center"
        >
          ≈{generated.entropyBits} bits of entropy across{" "}
          {generated.length} chars
        </p>
      ) : null}
    </form>
  )
}

// ─────────────────────────────────────────────────────────────────
// Page scaffold.
// ─────────────────────────────────────────────────────────────────

function ResetPasswordScaffold() {
  const search = useSearchParams()
  const level = useEffectiveMotionLevel()
  // The token is mandatory — without it the user can't authenticate
  // the rotation. Render a dedicated terminal card rather than a
  // form they could never submit successfully.
  const token = (search.get("token") || "").trim()

  return (
    <AuthVisualFoundation forceLevel={level}>
      <AuthGlassCard level={level}>
        {token ? (
          <ResetPasswordForm token={token} />
        ) : (
          <TokenMissingCard />
        )}
      </AuthGlassCard>
    </AuthVisualFoundation>
  )
}

export default function ResetPasswordPage() {
  return <ResetPasswordScaffold />
}
