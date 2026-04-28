"use client"

/**
 * AS.7.5 — Email verification page.
 *
 * Composes the AS.7.0 visual foundation + AS.7.1 brand-wordmark /
 * field-electric primitives. Two paths through the page:
 *
 *   1. **Magic-link verify** — user lands here as
 *      `/email-verify?token=<signed token>` after clicking the link
 *      from their inbox. The page auto-fires `auth.verifyEmail()` on
 *      mount and switches to a success / failure card based on the
 *      structured outcome:
 *        - `ok`               → VerifiedCard ("you're all set, sign in now")
 *        - `expired_token`    → ExpiredCard with embedded resend form
 *        - `invalid_token`    → InvalidCard with embedded resend form
 *        - `already_verified` → AlreadyVerifiedCard ("you can sign in now")
 *        - other failures     → ErrorCard with retry button
 *
 *   2. **Re-send link form** — user opens `/email-verify` directly
 *      (or lands here via the AS.7.2 signup terminal "check your
 *      inbox" CTA). The form takes an email and posts to
 *      `auth.resendEmailVerification()`. The success branch lands
 *      on the canonical "we sent another link to ..." terminal copy
 *      regardless of whether the email matched a known unverified
 *      user (enumeration-resistance contract per AS.0.7 §3.4).
 *
 * Visual features per the AS.7.5 row:
 *   - Envelope idle motion (Mail icon with `as7-envelope-idle` CSS
 *     class, 3.2s float-rotate keyframe gated to motion levels
 *     normal / dramatic via the `data-motion-level` cascade)
 *   - Resend form on every failure path so the user is never stuck
 *
 * Module-global state audit (per implement_phase_step.md SOP §1):
 * pure browser component. All state lives in React (`useState` /
 * `useRef`). The auth context is the SoT for `lastEmailVerifyError`
 * / `lastResendVerifyEmailError` / `user`. Per-tab / per-worker
 * derivation is trivially identical (Answer #1 of the SOP audit).
 *
 * Read-after-write timing audit: N/A — no DB, no parallelisation
 * change vs. existing auth-context behaviour.
 */

import { useEffect, useRef, useState } from "react"
import { useRouter, useSearchParams } from "next/navigation"
import {
  AlertCircle,
  ArrowLeft,
  ArrowRight,
  CheckCircle2,
  Loader2,
  Mail,
  RefreshCw,
} from "lucide-react"

import { useAuth } from "@/lib/auth-context"
import { useEffectiveMotionLevel } from "@/hooks/use-effective-motion-level"
import {
  AuthBrandWordmark,
  AuthFieldElectric,
  AuthGlassCard,
  AuthVisualFoundation,
} from "@/components/omnisight/auth"
import { bumpShakeKey } from "@/lib/auth/login-form-helpers"
import {
  EMAIL_VERIFY_ERROR_KIND,
  resendVerifyEmailSubmitBlockedReason,
} from "@/lib/auth/email-verify-helpers"

type VerifyStage = "verifying" | "verified" | "failed" | "idle"

// ─────────────────────────────────────────────────────────────────
// Reusable envelope icon with idle-motion CSS class.
// The CSS gates the keyframe to `[data-motion-level=normal|dramatic]`
// via the `as7-root` cascade, so we always emit the data attribute
// here and let the foundation root decide whether it animates.
// ─────────────────────────────────────────────────────────────────

function EnvelopeIcon({
  size = 36,
  active = true,
}: {
  size?: number
  active?: boolean
}) {
  return (
    <span
      data-testid="as7-verify-envelope"
      data-as7-envelope-idle={active ? "on" : "off"}
      className="as7-envelope-idle"
    >
      <Mail size={size} />
    </span>
  )
}

// ─────────────────────────────────────────────────────────────────
// Verifying spinner card — shown while the magic-link round-trip
// is in flight on initial mount.
// ─────────────────────────────────────────────────────────────────

function VerifyingCard({ level }: { level: ReturnType<typeof useEffectiveMotionLevel> }) {
  return (
    <div
      data-testid="as7-verify-verifying"
      className="flex flex-col gap-4 items-center text-center"
    >
      <AuthBrandWordmark level={level} />
      <EnvelopeIcon size={36} />
      <h1 className="font-mono text-base font-semibold text-[var(--foreground)]">
        Verifying your email…
      </h1>
      <p className="font-mono text-[12px] text-[var(--muted-foreground)] leading-relaxed">
        Hold tight — we&apos;re confirming the link from your inbox.
      </p>
      <Loader2
        size={18}
        className="animate-spin text-[var(--artifact-purple)]"
        aria-hidden="true"
      />
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// Terminal "all set" success card.
// ─────────────────────────────────────────────────────────────────

function VerifiedCard({
  email,
  level,
  next,
}: {
  email: string | null
  level: ReturnType<typeof useEffectiveMotionLevel>
  next: string
}) {
  const signInHref = `/login${
    next && next !== "/" ? `?next=${encodeURIComponent(next)}` : ""
  }`
  return (
    <div
      data-testid="as7-verify-success"
      className="flex flex-col gap-4 items-center text-center"
    >
      <AuthBrandWordmark level={level} />
      <CheckCircle2 size={32} className="text-emerald-500" />
      <h1 className="font-mono text-base font-semibold text-[var(--foreground)]">
        Email verified
      </h1>
      <p className="font-mono text-[12px] text-[var(--muted-foreground)] leading-relaxed">
        {email ? (
          <>
            <span className="text-[var(--foreground)]">{email}</span> is
            confirmed. You can sign in now and finish setting up your
            account.
          </>
        ) : (
          <>
            Your email is confirmed. You can sign in now and finish
            setting up your account.
          </>
        )}
      </p>
      <a
        href={signInHref}
        data-testid="as7-verify-signin-link"
        className="flex items-center gap-2 px-3 py-2 rounded bg-[var(--artifact-purple)] text-white font-mono text-xs font-semibold hover:opacity-90"
      >
        Sign in now <ArrowRight size={14} />
      </a>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// "Already verified" terminal card — friendlier copy than the
// generic invalid_token card since the user already proved control.
// ─────────────────────────────────────────────────────────────────

function AlreadyVerifiedCard({
  level,
  next,
}: {
  level: ReturnType<typeof useEffectiveMotionLevel>
  next: string
}) {
  const signInHref = `/login${
    next && next !== "/" ? `?next=${encodeURIComponent(next)}` : ""
  }`
  return (
    <div
      data-testid="as7-verify-already-verified"
      className="flex flex-col gap-4 items-center text-center"
    >
      <AuthBrandWordmark level={level} />
      <CheckCircle2 size={28} className="text-emerald-500" />
      <h1 className="font-mono text-base font-semibold text-[var(--foreground)]">
        You&apos;re already verified
      </h1>
      <p className="font-mono text-[12px] text-[var(--muted-foreground)] leading-relaxed">
        This email is already confirmed. Head over to sign in to
        continue.
      </p>
      <a
        href={signInHref}
        data-testid="as7-verify-signin-link"
        className="flex items-center gap-2 px-3 py-2 rounded bg-[var(--artifact-purple)] text-white font-mono text-xs font-semibold hover:opacity-90"
      >
        Sign in now <ArrowRight size={14} />
      </a>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// Resend-link terminal card — shown after a successful resend.
// ─────────────────────────────────────────────────────────────────

function LinkResentCard({
  email,
  level,
}: {
  email: string
  level: ReturnType<typeof useEffectiveMotionLevel>
}) {
  return (
    <div
      data-testid="as7-verify-link-resent"
      className="flex flex-col gap-4 items-center text-center"
    >
      <AuthBrandWordmark level={level} />
      <EnvelopeIcon size={32} />
      <h1 className="font-mono text-base font-semibold text-[var(--foreground)]">
        Check your inbox
      </h1>
      <p className="font-mono text-[12px] text-[var(--muted-foreground)] leading-relaxed">
        If an unverified account exists for{" "}
        <span className="text-[var(--foreground)]">{email}</span>, we sent
        a fresh verification link. The link expires in 30 minutes — open
        it from the same browser to confirm.
      </p>
      <p className="font-mono text-[10px] text-[var(--muted-foreground)] leading-relaxed">
        Didn&apos;t get it? Check your spam folder or wait a minute and
        request another link.
      </p>
      <a
        href="/login"
        data-testid="as7-verify-back-to-login"
        className="flex items-center gap-2 px-3 py-2 rounded bg-[var(--artifact-purple)] text-white font-mono text-xs font-semibold hover:opacity-90"
      >
        <ArrowLeft size={12} /> Back to sign in
      </a>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// Resend form — also serves as the "idle" entry path when no token
// is present in the URL, and as the embedded recovery surface on
// every failure path so users are never stuck.
// ─────────────────────────────────────────────────────────────────

interface ResendFormProps {
  level: ReturnType<typeof useEffectiveMotionLevel>
  initialEmail: string
  /** Optional banner shown above the form when arrived via a token-
   *  failure branch — drives the "verification link bad" copy. */
  failureKind: "expired_token" | "invalid_token" | "other" | null
  failureMessage: string | null
  onResent: (email: string) => void
}

function ResendForm({
  level,
  initialEmail,
  failureKind,
  failureMessage,
  onResent,
}: ResendFormProps) {
  const auth = useAuth()
  const [email, setEmail] = useState(initialEmail)
  const [busy, setBusy] = useState(false)
  const [errorKey, setErrorKey] = useState(0)
  const [bloomKey, setBloomKey] = useState(0)

  const submitBlockedReason = resendVerifyEmailSubmitBlockedReason({
    email,
    busy,
  })
  const submitDisabled = submitBlockedReason !== null

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (busy || submitBlockedReason !== null) return
    setBusy(true)
    try {
      const outcome = await auth.resendEmailVerification(email)
      if (outcome.status === "linkSent") {
        onResent(outcome.email ?? email)
      } else {
        setErrorKey(bumpShakeKey)
      }
    } finally {
      setBusy(false)
    }
  }

  const onFieldFocus = () => setBloomKey((k) => k + 1)
  const errorOutcome = auth.lastResendVerifyEmailError
  const showResendError = Boolean(errorOutcome) && !busy
  const showFailureBanner = Boolean(failureKind) && Boolean(failureMessage)

  return (
    <form
      onSubmit={onSubmit}
      data-testid="as7-verify-resend-form"
      data-as7-verify-failure-kind={failureKind ?? "none"}
      className="flex flex-col gap-4 relative"
    >
      <div className="flex flex-col items-center gap-1.5 text-center">
        <AuthBrandWordmark level={level} bloomKey={bloomKey} />
        <EnvelopeIcon size={28} />
        <h1 className="font-mono text-base font-semibold text-[var(--foreground)]">
          {failureKind === "expired_token"
            ? "Link expired"
            : failureKind === "invalid_token"
            ? "Link is no longer valid"
            : failureKind === "other"
            ? "Couldn't verify just now"
            : "Verify your email"}
        </h1>
        <p className="font-mono text-[11px] text-[var(--muted-foreground)] leading-relaxed">
          Enter the email you used at sign-up. We&apos;ll send a fresh
          verification link — it expires in 30 minutes.
        </p>
      </div>

      {showFailureBanner && failureMessage ? (
        <div
          role="alert"
          data-testid="as7-verify-failure-banner"
          data-as7-failure-kind={failureKind}
          className="flex items-start gap-2 p-2 rounded border border-[var(--destructive)] bg-[var(--destructive)]/10 text-[var(--destructive)] font-mono text-xs"
        >
          <AlertCircle size={14} className="shrink-0 mt-0.5" />
          <span>{failureMessage}</span>
        </div>
      ) : null}

      <AuthFieldElectric
        level={level}
        label="EMAIL"
        leadingIcon={<Mail size={14} />}
        hasError={showResendError}
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

      {showResendError && errorOutcome ? (
        <div
          role="alert"
          data-testid="as7-verify-resend-error"
          data-as7-resend-error-kind={errorOutcome.kind}
          className="flex items-start gap-2 p-2 rounded border border-[var(--destructive)] bg-[var(--destructive)]/10 text-[var(--destructive)] font-mono text-xs"
        >
          <AlertCircle size={14} className="shrink-0 mt-0.5" />
          <span>{errorOutcome.message}</span>
        </div>
      ) : null}

      <button
        type="submit"
        data-testid="as7-verify-resend-submit"
        disabled={submitDisabled}
        data-as7-block-reason={submitBlockedReason ?? "ok"}
        className="flex items-center justify-center gap-2 px-3 py-2 rounded bg-[var(--artifact-purple)] text-white font-mono text-sm font-semibold hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed"
      >
        {busy ? (
          <Loader2 size={14} className="animate-spin" />
        ) : (
          <RefreshCw size={14} />
        )}
        Send a fresh link
      </button>

      <p className="font-mono text-[10px] text-[var(--muted-foreground)] text-center leading-relaxed">
        Already verified?{" "}
        <a
          href="/login"
          data-testid="as7-verify-back-link"
          className="underline hover:text-[var(--foreground)] inline-flex items-center gap-1"
        >
          <ArrowLeft size={10} /> Back to sign in
        </a>
      </p>
    </form>
  )
}

// ─────────────────────────────────────────────────────────────────
// Page body — handles all four stages.
// ─────────────────────────────────────────────────────────────────

function EmailVerifyBody() {
  const auth = useAuth()
  const router = useRouter()
  const search = useSearchParams()
  const level = useEffectiveMotionLevel()

  const tokenFromUrl = (search.get("token") || "").trim()
  const initialEmail = search.get("email") || ""
  const next = search.get("next") || "/"

  const [stage, setStage] = useState<VerifyStage>(
    tokenFromUrl ? "verifying" : "idle",
  )
  const [verifiedEmail, setVerifiedEmail] = useState<string | null>(null)
  const [linkResentEmail, setLinkResentEmail] = useState<string | null>(null)
  const verifyRanRef = useRef(false)

  // Auto-verify on mount when ?token= is present. The ref guards
  // against StrictMode double-effect re-firing the request.
  useEffect(() => {
    if (verifyRanRef.current) return
    if (!tokenFromUrl) return
    verifyRanRef.current = true
    void (async () => {
      const outcome = await auth.verifyEmail({ token: tokenFromUrl })
      if (outcome.status === "ok") {
        setVerifiedEmail(outcome.email ?? null)
        setStage("verified")
      } else {
        setStage("failed")
      }
    })()
  }, [tokenFromUrl, auth])

  // If the user is already signed in and verified (some auth modes
  // redirect signed-in users away from /email-verify), bounce home.
  // Don't redirect on the success card — the user just signed up
  // and may want the explicit "Sign in now" CTA.
  useEffect(() => {
    if (auth.loading) return
    if (!auth.user) return
    if (stage === "verifying") return
    if (stage === "verified") return
    // Already signed in + landed without a token + idle stage → home.
    if (stage === "idle" && !tokenFromUrl) {
      router.replace(next)
    }
  }, [auth.loading, auth.user, stage, tokenFromUrl, next, router])

  if (stage === "verifying") {
    return <VerifyingCard level={level} />
  }

  if (stage === "verified") {
    return (
      <VerifiedCard email={verifiedEmail} level={level} next={next} />
    )
  }

  // Already-verified is an explicit branch on the failed path.
  const failureKindFromContext = auth.lastEmailVerifyError?.kind ?? null
  if (
    stage === "failed" &&
    failureKindFromContext === EMAIL_VERIFY_ERROR_KIND.alreadyVerified
  ) {
    return <AlreadyVerifiedCard level={level} next={next} />
  }

  if (linkResentEmail) {
    return <LinkResentCard email={linkResentEmail} level={level} />
  }

  // Failure / idle — render the resend form. The failure banner is
  // only shown on the failure stage; the idle stage (no token) shows
  // a clean form.
  let failureKind: ResendFormProps["failureKind"] = null
  let failureMessage: string | null = null
  if (stage === "failed" && auth.lastEmailVerifyError) {
    if (
      failureKindFromContext === EMAIL_VERIFY_ERROR_KIND.expiredToken
    ) {
      failureKind = "expired_token"
    } else if (
      failureKindFromContext === EMAIL_VERIFY_ERROR_KIND.invalidToken
    ) {
      failureKind = "invalid_token"
    } else {
      failureKind = "other"
    }
    failureMessage = auth.lastEmailVerifyError.message
  }

  return (
    <ResendForm
      level={level}
      initialEmail={initialEmail}
      failureKind={failureKind}
      failureMessage={failureMessage}
      onResent={(email) => setLinkResentEmail(email)}
    />
  )
}

// ─────────────────────────────────────────────────────────────────
// Page scaffold — same shape as AS.7.1 / AS.7.2 / AS.7.3 / AS.7.4.
// ─────────────────────────────────────────────────────────────────

function EmailVerifyScaffold() {
  const level = useEffectiveMotionLevel()
  return (
    <AuthVisualFoundation forceLevel={level}>
      <AuthGlassCard level={level}>
        <EmailVerifyBody />
      </AuthGlassCard>
    </AuthVisualFoundation>
  )
}

export default function EmailVerifyPage() {
  return <EmailVerifyScaffold />
}
