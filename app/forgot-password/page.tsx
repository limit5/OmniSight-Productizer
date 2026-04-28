"use client"

/**
 * AS.7.3 — Forgot password page (stage 1/2 of password reset).
 *
 * Composes the AS.7.0 visual foundation + AS.7.1 login primitives
 * for the email-submission stage of the password-reset flow. The
 * page is enumeration-resistant: regardless of whether the email
 * matched a known account the success branch always renders the
 * same "if your account exists, we've sent a link" terminal copy
 * (the AS.0.7 §3.4 contract). Only genuine failure modes (rate-
 * limit / bot-challenge / 5xx) surface a user-visible error.
 *
 * Why a separate page instead of an inline modal on /login: the
 * design cited in the AS.7 epic notes asks for a dedicated route
 * so users can deep-link / refresh without losing context, and the
 * 2-stage flow (request → magic-link → confirm) makes it natural
 * for `/forgot-password` and `/reset-password` to be sister pages.
 *
 * Module-global state audit (per implement_phase_step.md SOP §1):
 * pure browser component. All state lives in React (`useState` /
 * `useRef`). The auth context is the SoT for user / error state.
 * Per-tab / per-worker derivation is trivially identical (Answer
 * #1 of the SOP audit).
 *
 * Read-after-write timing audit: N/A — no DB, no parallelisation
 * change vs. existing auth-context behaviour.
 */

import { useEffect, useRef, useState } from "react"
import { useRouter, useSearchParams } from "next/navigation"
import {
  AlertCircle,
  ArrowLeft,
  KeyRound,
  Loader2,
  Mail,
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
} from "@/components/omnisight/auth"
import { bumpShakeKey } from "@/lib/auth/login-form-helpers"
import {
  passwordResetHoneypotFieldName,
  requestResetSubmitBlockedReason,
} from "@/lib/auth/password-reset-helpers"

const TURNSTILE_SITE_KEY: string | null =
  process.env.NEXT_PUBLIC_TURNSTILE_SITE_KEY ?? null

// ─────────────────────────────────────────────────────────────────
// Terminal "check your inbox" card.
// ─────────────────────────────────────────────────────────────────

function LinkSentCard({ email }: { email: string }) {
  return (
    <div
      data-testid="as7-forgot-link-sent"
      className="flex flex-col gap-4 items-center text-center"
    >
      <Mail size={32} className="text-[var(--artifact-purple)]" />
      <h1 className="font-mono text-base font-semibold text-[var(--foreground)]">
        Check your inbox
      </h1>
      <p className="font-mono text-[12px] text-[var(--muted-foreground)] leading-relaxed">
        If an account exists for{" "}
        <span className="text-[var(--foreground)]">{email}</span>, we
        sent a password reset link. The link expires in 30 minutes —
        click it from the same browser to set a new password.
      </p>
      <p className="font-mono text-[10px] text-[var(--muted-foreground)] leading-relaxed">
        Didn&apos;t get it? Check your spam folder or wait a minute and
        request a fresh link.
      </p>
      <a
        href="/login"
        data-testid="as7-forgot-back-to-login"
        className="flex items-center gap-2 px-3 py-2 rounded bg-[var(--artifact-purple)] text-white font-mono text-xs font-semibold hover:opacity-90"
      >
        <ArrowLeft size={12} /> Back to sign in
      </a>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// Forgot-password form (default path).
// ─────────────────────────────────────────────────────────────────

function ForgotPasswordForm() {
  const router = useRouter()
  const search = useSearchParams()
  const auth = useAuth()
  const level = useEffectiveMotionLevel()

  // Pre-fill from `?email=` if the user came from the login page
  // with their email already typed (we forward it so they don't have
  // to retype). The login link sets `?email=...&next=...`.
  const initialEmail = search.get("email") || ""
  const next = search.get("next") || "/"

  const [email, setEmail] = useState(initialEmail)
  const [busy, setBusy] = useState(false)
  const [errorKey, setErrorKey] = useState(0)
  const [bloomKey, setBloomKey] = useState(0)
  const [turnstileToken, setTurnstileToken] = useState<string | null>(null)
  const [linkSent, setLinkSent] = useState<string | null>(null)
  const honeypotFieldRef = useRef<string | null>(null)
  const honeypotResolvedRef = useRef<boolean>(false)
  const [, setHoneypotTick] = useState(0)
  const honeypotResolved = honeypotResolvedRef.current

  // If the user is already signed in, send them home — they
  // shouldn't be on the password-reset page in that state.
  useEffect(() => {
    if (!auth.loading && auth.user) router.replace(next)
  }, [auth.loading, auth.user, next, router])

  // Pre-fetch the rotating honeypot field name so the AS.7.3 form
  // path's `pr_*` prefix lands in `honeypotFieldRef` before the
  // user can submit. Mirrors the AS.7.2 signup-page pattern.
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
        // Web Crypto unavailable. Leave resolved=false; the gate
        // keeps submit disabled.
      })
    return () => {
      cancelled = true
    }
  }, [])

  const onFieldFocus = () => setBloomKey((k) => k + 1)

  const submitBlockedReason = requestResetSubmitBlockedReason({
    email,
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
      const outcome = await auth.requestPasswordReset(email, extras)
      if (outcome.status === "linkSent") {
        setLinkSent(outcome.email ?? email)
      } else {
        setErrorKey(bumpShakeKey)
      }
    } finally {
      setBusy(false)
    }
  }

  if (linkSent) {
    return <LinkSentCard email={linkSent} />
  }

  const errorOutcome = auth.lastRequestResetError
  const hasError = Boolean(errorOutcome)
  const submitDisabled = submitBlockedReason !== null

  return (
    <form
      onSubmit={onSubmit}
      data-testid="as7-forgot-form"
      className="flex flex-col gap-4 relative"
    >
      <div className="flex flex-col items-center gap-1.5 text-center">
        <AuthBrandWordmark level={level} bloomKey={bloomKey} />
        <KeyRound size={22} className="text-[var(--artifact-purple)]" />
        <h1 className="font-mono text-base font-semibold text-[var(--foreground)]">
          Forgot your password?
        </h1>
        <p className="font-mono text-[11px] text-[var(--muted-foreground)] leading-relaxed">
          Enter the email associated with your account. We&apos;ll send
          you a link to set a new password.
        </p>
      </div>

      <AuthFieldElectric
        level={level}
        label="EMAIL"
        leadingIcon={<Mail size={14} />}
        hasError={hasError}
        errorKey={errorKey}
        inputProps={{
          name: "email",
          type: "email",
          autoComplete: "email",
          autoFocus: true,
          required: true,
          placeholder: "you@example.com",
          value: email,
          onChange: (e) => setEmail(e.target.value),
          onFocus: onFieldFocus,
        }}
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
            action="password_reset"
            onToken={(token) => setTurnstileToken(token)}
            onExpired={() => setTurnstileToken(null)}
            onError={() => setTurnstileToken(null)}
          />
        </div>
      ) : null}

      {hasError && errorOutcome ? (
        <div
          role="alert"
          data-testid="as7-forgot-error"
          className="flex items-start gap-2 p-2 rounded border border-[var(--destructive)] bg-[var(--destructive)]/10 text-[var(--destructive)] font-mono text-xs"
        >
          <AlertCircle size={14} className="shrink-0 mt-0.5" />
          <span>{errorOutcome.message}</span>
        </div>
      ) : null}

      <button
        type="submit"
        data-testid="as7-forgot-submit"
        disabled={submitDisabled}
        data-as7-block-reason={submitBlockedReason ?? "ok"}
        className="flex items-center justify-center gap-2 px-3 py-2 rounded bg-[var(--artifact-purple)] text-white font-mono text-sm font-semibold hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed"
      >
        {busy ? <Loader2 size={14} className="animate-spin" /> : null}
        Send reset link
      </button>

      <p className="font-mono text-[10px] text-[var(--muted-foreground)] text-center leading-relaxed">
        Remembered it?{" "}
        <a
          href="/login"
          data-testid="as7-forgot-back-link"
          className="underline hover:text-[var(--foreground)] inline-flex items-center gap-1"
        >
          <ArrowLeft size={10} /> Back to sign in
        </a>
      </p>
    </form>
  )
}

// ─────────────────────────────────────────────────────────────────
// Page scaffold — same shape as AS.7.1 / AS.7.2.
// ─────────────────────────────────────────────────────────────────

function ForgotPasswordScaffold() {
  const level = useEffectiveMotionLevel()
  return (
    <AuthVisualFoundation forceLevel={level}>
      <AuthGlassCard level={level}>
        <ForgotPasswordForm />
      </AuthGlassCard>
    </AuthVisualFoundation>
  )
}

export default function ForgotPasswordPage() {
  return <ForgotPasswordScaffold />
}
