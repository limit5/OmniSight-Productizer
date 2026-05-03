"use client"

/**
 * AS.7.6 — Account locked / suspended dedicated page.
 *
 * Composes the AS.7.0 visual foundation (`<AuthVisualFoundation>` +
 * `<AuthGlassCard>`) with a chill blue-tint hero block + lock kind-
 * driven copy + recovery-path CTA row. Three lockout kinds the page
 * branches on (vocabulary pinned in
 * `lib/auth/account-locked-helpers.ts`):
 *
 *   1. **temporary_lockout** — repeated failed login attempts. Shows
 *      a live countdown + "Try signing in again" CTA that becomes
 *      enabled when the timer hits zero.
 *   2. **admin_suspended** — administrator manually disabled the
 *      account. No countdown — only the contact-admin path.
 *   3. **security_hold** — defensive default for any 423 / security
 *      event without a more-specific reason hint. Surfaces the
 *      forgot-password CTA + contact-admin path.
 *
 * Inputs merged via the helper's precedence cascade:
 *   - Live `auth.lastLoginError` (just-now 423)
 *   - URL query: `?reason=&retry_after=&next=&email=`
 *   - Defensive default
 *
 * Visual features per the AS.7.6 row:
 *   - Blue tint (reuses the AS.7.1 `.as7-account-locked-frost`
 *     gradient so the hero block matches the inline overlay)
 *   - Chill shimmer on the snowflake icon (reuses the AS.7.1
 *     `as7-chill-shimmer` keyframe gated to motion levels normal /
 *     dramatic via the `data-motion-level` cascade)
 *   - Slow-pulse breathing on the title (reuses the AS.7.0
 *     `as7-breathing-pulse`)
 *
 * Module-global state audit (per implement_phase_step.md SOP §1):
 * pure browser component. All state lives in React (`useState` /
 * `useRef` / `useEffect`). The auth context is the SoT for
 * `lastLoginError` / `user`. Per-tab / per-worker derivation is
 * trivially identical (Answer #1 of the SOP audit) — the helper is
 * pure.
 *
 * Read-after-write timing audit: N/A — no DB, no parallelisation
 * change vs. existing auth-context behaviour.
 */

import { useEffect, useMemo, useState } from "react"
import { useRouter, useSearchParams } from "next/navigation"
import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  KeyRound,
  Lock,
  LogOut,
  Mail,
  Snowflake,
} from "lucide-react"

import { useAuth } from "@/lib/auth-context"
import { useEffectiveMotionLevel } from "@/hooks/use-effective-motion-level"
import {
  AuthBrandWordmark,
  AuthGlassCard,
  AuthVisualFoundation,
} from "@/components/omnisight/auth"
import {
  DEFAULT_ADMIN_CONTACT_EMAIL,
  LOCKOUT_REASON_KIND,
  buildContactAdminMailto,
  formatRemainingTime,
  lockoutEffectiveState,
  retrySignInBlockedReason,
  type LockoutEffectiveState,
} from "@/lib/auth/account-locked-helpers"

// Resolved at build time. Falls back to the canonical default
// `admin@omnisight.local` (matches the backend bootstrap default)
// when the operator hasn't set a contact email — every deployment
// still emits a plausible mailto: rather than an empty href.
const ADMIN_CONTACT_EMAIL: string =
  process.env.NEXT_PUBLIC_OMNISIGHT_ADMIN_EMAIL || DEFAULT_ADMIN_CONTACT_EMAIL

// ─────────────────────────────────────────────────────────────────
// Hero block — chill blue-tint frost + snowflake icon + title.
// Reuses the AS.7.1 `.as7-account-locked-*` selectors so the visual
// is byte-equal to the inline overlay shown on /login during the 423
// branch (the dedicated page is the canonical surface; the inline
// overlay is the one-frame transition before redirect).
// ─────────────────────────────────────────────────────────────────

function LockoutHero({
  state,
  remainingSeconds,
  level,
}: {
  state: LockoutEffectiveState
  remainingSeconds: number | null
  level: ReturnType<typeof useEffectiveMotionLevel>
}) {
  return (
    <div
      data-testid="as7-locked-hero"
      data-as7-locked-kind={state.kind}
      className="as7-locked-hero"
    >
      <div className="as7-locked-hero-frost" aria-hidden="true" />
      <div className="as7-locked-hero-content">
        <AuthBrandWordmark level={level} />
        <span
          className="as7-locked-hero-icon"
          data-as7-chill={
            level === "off" || level === "subtle" ? "off" : "on"
          }
          aria-hidden="true"
        >
          <Snowflake size={44} />
        </span>
        <h1
          data-testid="as7-locked-title"
          className="as7-locked-hero-title"
        >
          {state.copy.title}
        </h1>
        <p
          data-testid="as7-locked-summary"
          className="as7-locked-hero-summary"
        >
          {state.copy.summary}
        </p>
        {state.supportsCountdown && remainingSeconds !== null ? (
          <p
            data-testid="as7-locked-countdown"
            data-as7-locked-countdown-active={
              remainingSeconds > 0 ? "yes" : "no"
            }
            aria-live="polite"
            className="as7-locked-hero-countdown"
          >
            {remainingSeconds > 0 ? (
              <>
                Retry available in{" "}
                <span className="as7-locked-hero-countdown-value">
                  {formatRemainingTime(remainingSeconds)}
                </span>
              </>
            ) : (
              <>You can try signing in again now.</>
            )}
          </p>
        ) : null}
      </div>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// Recovery row — the page's three primary CTAs gated by the kind's
// capabilities table.
// ─────────────────────────────────────────────────────────────────

interface RecoveryRowProps {
  state: LockoutEffectiveState
  remainingSeconds: number | null
  next: string
  onSignOut: (() => void) | null
}

function RecoveryRow({
  state,
  remainingSeconds,
  next,
  onSignOut,
}: RecoveryRowProps) {
  const retryBlocked = retrySignInBlockedReason({
    state,
    remainingSeconds,
  })
  const retryDisabled = retryBlocked !== null
  const loginHref = `/login${
    state.email
      ? `?next=${encodeURIComponent(next)}&email=${encodeURIComponent(state.email)}`
      : `?next=${encodeURIComponent(next)}`
  }`
  const forgotHref = `/forgot-password${
    state.email ? `?email=${encodeURIComponent(state.email)}` : ""
  }`
  const mailtoHref = buildContactAdminMailto({
    adminEmail: ADMIN_CONTACT_EMAIL,
    userEmail: state.email,
    kind: state.kind,
  })

  return (
    <div
      data-testid="as7-locked-recovery-row"
      className="flex flex-col gap-3"
    >
      {state.supportsRetrySignIn ? (
        <a
          // FX.7.12: href stays bound to loginHref even when disabled so
          // the anchor is a valid link (jsx-a11y/anchor-is-valid). When
          // retryDisabled, the onClick prevents navigation; aria-disabled
          // + tabIndex=-1 communicate the disabled state to AT users and
          // remove it from keyboard focus order.
          href={loginHref}
          data-testid="as7-locked-retry-signin"
          data-as7-block-reason={retryBlocked ?? "ok"}
          aria-disabled={retryDisabled}
          tabIndex={retryDisabled ? -1 : undefined}
          onClick={(e) => {
            if (retryDisabled) e.preventDefault()
          }}
          className={
            "flex items-center justify-center gap-2 px-3 py-2 rounded font-mono text-sm font-semibold transition-opacity " +
            (retryDisabled
              ? "bg-[var(--artifact-purple)]/40 text-white/70 cursor-not-allowed"
              : "bg-[var(--artifact-purple)] text-white hover:opacity-90")
          }
        >
          <KeyRound size={14} />
          {retryDisabled ? "Try signing in again" : "Try signing in again"}
          {!retryDisabled ? <ArrowRight size={14} /> : null}
        </a>
      ) : null}

      {state.supportsResetPassword ? (
        <a
          href={forgotHref}
          data-testid="as7-locked-reset-password"
          className="flex items-center justify-center gap-2 px-3 py-2 rounded border border-[var(--border)] text-[var(--foreground)] font-mono text-xs hover:border-[var(--artifact-purple)] hover:text-[var(--artifact-purple)]"
        >
          <Lock size={12} />
          Reset password
        </a>
      ) : null}

      {state.supportsContactAdmin ? (
        <a
          href={mailtoHref}
          data-testid="as7-locked-contact-admin"
          data-as7-admin-email={ADMIN_CONTACT_EMAIL}
          className="flex items-center justify-center gap-2 px-3 py-2 rounded border border-[var(--border)] text-[var(--foreground)] font-mono text-xs hover:border-[var(--artifact-purple)] hover:text-[var(--artifact-purple)]"
        >
          <Mail size={12} />
          Contact administrator
        </a>
      ) : null}

      <p
        data-testid="as7-locked-recovery-hint"
        className="font-mono text-[10px] text-[var(--muted-foreground)] text-center leading-relaxed"
      >
        {state.copy.recoveryHint}
      </p>

      {onSignOut ? (
        <button
          type="button"
          onClick={onSignOut}
          data-testid="as7-locked-sign-out"
          className="flex items-center justify-center gap-1 font-mono text-[10px] text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
        >
          <LogOut size={10} />
          Sign out of OmniSight
        </button>
      ) : null}

      <a
        href="/login"
        data-testid="as7-locked-back-link"
        className="flex items-center justify-center gap-1 font-mono text-[10px] text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
      >
        <ArrowLeft size={10} />
        Back to sign in
      </a>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// Page body — wires the helper inputs + countdown timer + sign-out.
// ─────────────────────────────────────────────────────────────────

function AccountLockedBody() {
  const auth = useAuth()
  const router = useRouter()
  const search = useSearchParams()
  const level = useEffectiveMotionLevel()

  const reasonHint = search.get("reason")
  const retryAfterRaw = search.get("retry_after")
  const emailHint = search.get("email")
  const next = search.get("next") || "/"

  const state = useMemo(
    () =>
      lockoutEffectiveState({
        reasonHint,
        retryAfterRaw,
        emailHint,
        liveLoginError: auth.lastLoginError
          ? {
              accountLocked: Boolean(auth.lastLoginError.accountLocked),
              retryAfterSeconds: auth.lastLoginError.retryAfterSeconds ?? null,
            }
          : null,
        liveUserEmail: auth.user?.email ?? null,
      }),
    [
      reasonHint,
      retryAfterRaw,
      emailHint,
      auth.lastLoginError,
      auth.user,
    ],
  )

  // Live countdown — initial value comes from the helper's resolved
  // retryAfterSeconds; a setInterval ticks it down once per second.
  const [remainingSeconds, setRemainingSeconds] = useState<number | null>(
    state.supportsCountdown ? state.retryAfterSeconds : null,
  )
  useEffect(() => {
    if (!state.supportsCountdown) {
      setRemainingSeconds(null)
      return
    }
    const start = state.retryAfterSeconds
    if (start === null) {
      setRemainingSeconds(null)
      return
    }
    setRemainingSeconds(start)
    if (start <= 0) return
    const timer = window.setInterval(() => {
      setRemainingSeconds((prev) => {
        if (prev === null) return null
        return prev > 1 ? prev - 1 : 0
      })
    }, 1000)
    return () => window.clearInterval(timer)
  }, [state.supportsCountdown, state.retryAfterSeconds])

  // Sign-out CTA — only rendered when an authenticated session is
  // somehow still alive on this page (rare; happens on the security-
  // hold path when the dashboard redirects an authenticated user
  // here after observing a 423 from a backend RPC).
  const onSignOut =
    auth.user !== null
      ? async () => {
          await auth.logout()
          router.replace(`/login?next=${encodeURIComponent(next)}`)
        }
      : null

  // Banner emitted only on the security-hold kind — calls explicit
  // attention to the reason a fresh password reset is needed.
  const showSecurityBanner =
    state.kind === LOCKOUT_REASON_KIND.securityHold

  return (
    <div
      data-testid="as7-locked-body"
      data-as7-locked-kind={state.kind}
      className="flex flex-col gap-5"
    >
      <LockoutHero
        state={state}
        remainingSeconds={remainingSeconds}
        level={level}
      />

      {showSecurityBanner ? (
        <div
          role="alert"
          data-testid="as7-locked-security-banner"
          className="flex items-start gap-2 p-2 rounded border border-[var(--artifact-purple)] bg-[var(--artifact-purple)]/10 text-[var(--foreground)] font-mono text-xs"
        >
          <AlertTriangle
            size={14}
            className="shrink-0 mt-0.5 text-[var(--artifact-purple)]"
          />
          <span>
            A security event triggered this hold. Resetting your password
            removes the hold.
          </span>
        </div>
      ) : null}

      <RecoveryRow
        state={state}
        remainingSeconds={remainingSeconds}
        next={next}
        onSignOut={onSignOut}
      />
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// Page scaffold — same shape as AS.7.1 / AS.7.2 / AS.7.3 / AS.7.4 /
// AS.7.5. Resolves motion level once at root + threads down.
// ─────────────────────────────────────────────────────────────────

function AccountLockedScaffold() {
  const level = useEffectiveMotionLevel()
  return (
    <AuthVisualFoundation forceLevel={level}>
      <AuthGlassCard level={level}>
        <AccountLockedBody />
      </AuthGlassCard>
    </AuthVisualFoundation>
  )
}

export default function AccountLockedPage() {
  return <AccountLockedScaffold />
}
