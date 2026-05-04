"use client"

/**
 * AS.7.4 — Dedicated MFA challenge page.
 *
 * Replaces the inline MFA form previously housed inside
 * `app/login/page.tsx::<MfaChallengeForm>` with a full-page
 * experience that composes the AS.7.0 visual foundation +
 * AS.7.1 login primitives + AS.7.4 MFA primitives.
 *
 * Flow:
 *   1. Login page calls `auth.login()`. On `mfa_required` it
 *      `setMfaPending(...)` in context AND `router.push("/mfa-challenge")`.
 *   2. This page reads `auth.mfaPending` from context. If unset
 *      (refresh / direct visit) it sends the user back to /login.
 *   3. The user picks a method tab (TOTP / WebAuthn / backup code)
 *      and submits.
 *   4. On success: `<MfaPassedCheck>` plays for 0.6 / 0.9s, then
 *      `router.replace(next)`.
 *   5. On expired-challenge failure: bounce to /login with a
 *      banner (auth context cancels mfaPending so the next /login
 *      visit shows the credentials form fresh).
 *   6. On retryable failure: stay on the page, surface the error
 *      banner + reset the input.
 *
 * Visual features per the AS.7.4 TODO row:
 *   - 6-digit pulse animation (`<MfaCodePulse>`) — each digit is a
 *     cell with a brand-purple glow that pulses on insertion.
 *   - ✓ passed animation (`<MfaPassedCheck>`) — fullscreen overlay
 *     between the resolve and the navigation.
 *   - Backup-code path (when TOTP is enrolled, the user can enter
 *     `xxxx-xxxx` instead — backend dispatches in the same endpoint).
 *   - WebAuthn / passkey path via `navigator.credentials.get()`
 *     orchestrated by `auth.submitMfaWebauthn()`.
 *
 * Module-global state audit (per implement_phase_step.md SOP §1):
 * pure browser component. All state lives in React (`useState` /
 * `useRef`). The auth context is the SoT for `mfaPending` /
 * `lastMfaChallengeError` / `user`. Per-tab / per-worker derivation
 * is trivially identical (Answer #1 of the SOP audit).
 *
 * Read-after-write timing audit: N/A — no DB, no parallelisation
 * change vs. existing auth-context behaviour.
 */

import { useEffect, useMemo, useRef, useState } from "react"
import { useRouter, useSearchParams } from "next/navigation"
import { useTranslations } from "next-intl"
import {
  AlertCircle,
  ArrowLeft,
  Fingerprint,
  KeyRound,
  Loader2,
  Shield,
  ShieldAlert,
  ShieldCheck,
} from "lucide-react"

import { useAuth } from "@/lib/auth-context"
import { useEffectiveMotionLevel } from "@/hooks/use-effective-motion-level"
import {
  AuthBrandWordmark,
  AuthGlassCard,
  AuthVisualFoundation,
  MfaCodePulse,
  MfaMethodTabs,
  MfaPassedCheck,
} from "@/components/omnisight/auth"
import {
  BACKUP_CODE_LENGTH,
  MFA_METHOD_KIND,
  TOTP_CODE_LENGTH,
  bumpPulseKey,
  mfaChallengeSubmitBlockedReason,
  normaliseMfaInput,
  selectableMethods,
  type MfaMethodKind,
} from "@/lib/auth/mfa-challenge-helpers"

// ─────────────────────────────────────────────────────────────────
// Method-specific input panel.
// ─────────────────────────────────────────────────────────────────

interface MethodPanelProps {
  kind: MfaMethodKind
  level: "off" | "subtle" | "normal" | "dramatic"
  value: string
  pulseKey: number
  passed: boolean
  onValueChange: (next: string, normalised: string) => void
  onWebauthnClick: () => void
  busy: boolean
  webauthnInFlight: boolean
}

function MethodPanel({
  kind,
  level,
  value,
  pulseKey,
  passed,
  onValueChange,
  onWebauthnClick,
  busy,
  webauthnInFlight,
}: MethodPanelProps) {
  const tMfa = useTranslations("mfa")
  if (kind === MFA_METHOD_KIND.totp) {
    return (
      <div
        role="tabpanel"
        id="as7-mfa-panel-totp"
        data-testid="as7-mfa-panel-totp"
        className="flex flex-col items-center gap-3"
      >
        <MfaCodePulse
          level={level}
          value={value}
          pulseKey={pulseKey}
          passed={passed}
        />
        <input
          data-testid="as7-mfa-totp-input"
          aria-label={tMfa("totpAriaLabel")}
          inputMode="numeric"
          autoComplete="one-time-code"
          autoFocus
          maxLength={TOTP_CODE_LENGTH}
          value={value}
          onChange={(e) => {
            const raw = e.target.value
            const normalised = normaliseMfaInput(MFA_METHOD_KIND.totp, raw)
            onValueChange(raw, normalised)
          }}
          placeholder="000000"
          className="font-mono text-center text-lg tracking-[0.4em] py-2 px-3 w-full rounded border border-[var(--border)] bg-[var(--background)]/40 focus:outline-none focus:ring-1 focus:ring-[var(--artifact-purple)]"
          disabled={busy}
        />
      </div>
    )
  }
  if (kind === MFA_METHOD_KIND.backupCode) {
    return (
      <div
        role="tabpanel"
        id="as7-mfa-panel-backup_code"
        data-testid="as7-mfa-panel-backup_code"
        className="flex flex-col items-center gap-3"
      >
        <KeyRound size={20} className="text-[var(--artifact-purple)]" />
        <input
          data-testid="as7-mfa-backup-input"
          aria-label={tMfa("backupAriaLabel")}
          autoComplete="one-time-code"
          autoFocus
          maxLength={BACKUP_CODE_LENGTH}
          value={value}
          onChange={(e) => {
            const raw = e.target.value
            const normalised = normaliseMfaInput(
              MFA_METHOD_KIND.backupCode,
              raw,
            )
            onValueChange(raw, normalised)
          }}
          placeholder="xxxx-xxxx"
          className="font-mono text-center text-base tracking-widest py-2 px-3 w-full rounded border border-[var(--border)] bg-[var(--background)]/40 focus:outline-none focus:ring-1 focus:ring-[var(--artifact-purple)]"
          disabled={busy}
        />
      </div>
    )
  }
  return (
    <div
      role="tabpanel"
      id="as7-mfa-panel-webauthn"
      data-testid="as7-mfa-panel-webauthn"
      className="flex flex-col items-center gap-3"
    >
      <Fingerprint
        size={36}
        className="text-[var(--artifact-purple)]"
      />
      <p className="font-mono text-[11px] text-[var(--muted-foreground)] text-center leading-relaxed">
        {tMfa("tapKey")}
      </p>
      <button
        type="button"
        data-testid="as7-mfa-webauthn-go"
        onClick={onWebauthnClick}
        disabled={busy || webauthnInFlight}
        className="flex items-center justify-center gap-2 px-3 py-2 w-full rounded bg-[var(--artifact-purple)] text-white font-mono text-sm font-semibold hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed"
      >
        {webauthnInFlight ? (
          <Loader2 size={14} className="animate-spin" />
        ) : (
          <ShieldCheck size={14} />
        )}
        {tMfa("useSecurityKey")}
      </button>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// Form body.
// ─────────────────────────────────────────────────────────────────

function MfaChallengeForm() {
  const auth = useAuth()
  const router = useRouter()
  const search = useSearchParams()
  const next = search.get("next") || "/"
  const level = useEffectiveMotionLevel()
  const tMfa = useTranslations("mfa")
  const tAuth = useTranslations("auth")

  const [value, setValue] = useState("")
  const [pulseKey, setPulseKey] = useState(0)
  const [busy, setBusy] = useState(false)
  const [webauthnInFlight, setWebauthnInFlight] = useState(false)
  const [passed, setPassed] = useState(false)
  const [bloomKey, setBloomKey] = useState(0)

  const offeredMethods = useMemo(
    () => selectableMethods(auth.mfaPending?.mfa_methods || []),
    [auth.mfaPending],
  )

  const [kind, setKind] = useState<MfaMethodKind>(
    () => offeredMethods[0] ?? MFA_METHOD_KIND.totp,
  )

  // Re-anchor the active tab if the offered methods change between
  // mounts (e.g., the operator just completed WebAuthn enrolment in
  // another tab while this tab was showing the credentials form).
  useEffect(() => {
    if (!offeredMethods.includes(kind)) {
      setKind(offeredMethods[0] ?? MFA_METHOD_KIND.totp)
      setValue("")
    }
  }, [offeredMethods, kind])

  // If the auth context already has a user signed in (the parent
  // login page never set mfaPending OR a refresh fired between
  // login + this page), bounce home.
  useEffect(() => {
    if (!auth.loading && auth.user && !passed) {
      router.replace(next)
    }
  }, [auth.loading, auth.user, next, router, passed])

  // If mfaPending was lost (refresh wipes the in-memory state), or
  // the user opened this page directly without a pending challenge,
  // bounce back to /login. Defer until after `loading` settles so
  // the initial whoami doesn't trigger a false redirect.
  useEffect(() => {
    if (auth.loading) return
    if (auth.mfaPending) return
    if (auth.user) return
    if (passed) return
    router.replace(`/login?next=${encodeURIComponent(next)}`)
  }, [auth.loading, auth.mfaPending, auth.user, next, router, passed])

  const submitBlockedReason = mfaChallengeSubmitBlockedReason({
    kind,
    value,
    busy,
  })
  const submitDisabled =
    submitBlockedReason !== null && kind !== MFA_METHOD_KIND.webauthn

  const onValueChange = (raw: string, normalised: string) => {
    // Only bump the pulse key when the normalised value actually
    // grew — pasting the same value or backspacing should not replay
    // the cell pulse.
    setValue((prev) => {
      const next = kind === MFA_METHOD_KIND.totp ? normalised : raw
      if (kind === MFA_METHOD_KIND.totp && next.length > prev.length) {
        setPulseKey(bumpPulseKey)
      }
      return next
    })
  }

  const playPassedThenNavigate = () => {
    setPassed(true)
    // <MfaPassedCheck>'s onComplete handler does the actual
    // navigation. Off / subtle motion levels skip the overlay and
    // the callback fires on the next microtask.
  }

  const onPassedComplete = () => {
    router.replace(next)
  }

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (busy) return
    if (kind === MFA_METHOD_KIND.webauthn) return
    if (submitBlockedReason !== null) return
    setBusy(true)
    try {
      const outcome = await auth.submitMfaStructured(value)
      if (outcome.status === "ok") {
        playPassedThenNavigate()
        return
      }
      // Failure — clear the input so the user can retype. Expired
      // challenge bounces back to /login.
      setValue("")
      setPulseKey(bumpPulseKey)
      if (outcome.error?.kind === "expired_challenge") {
        auth.cancelMfa()
        router.replace(`/login?next=${encodeURIComponent(next)}`)
      }
    } finally {
      setBusy(false)
    }
  }

  const onWebauthn = async () => {
    if (webauthnInFlight) return
    setWebauthnInFlight(true)
    try {
      const outcome = await auth.submitMfaWebauthn()
      if (outcome.status === "ok") {
        playPassedThenNavigate()
        return
      }
      if (outcome.error?.kind === "expired_challenge") {
        auth.cancelMfa()
        router.replace(`/login?next=${encodeURIComponent(next)}`)
      }
    } finally {
      setWebauthnInFlight(false)
    }
  }

  const onCancel = () => {
    auth.cancelMfa()
    router.replace(`/login?next=${encodeURIComponent(next)}`)
  }

  const onFieldFocus = () => setBloomKey((k) => k + 1)

  const errorOutcome = auth.lastMfaChallengeError
  const showError = Boolean(errorOutcome) && !busy && !passed

  // No mfaPending and not yet bounced — render a holding state so
  // we don't flash a blank glass card.
  if (!auth.mfaPending) {
    return (
      <div
        data-testid="as7-mfa-no-challenge"
        className="flex flex-col items-center gap-3 text-center"
      >
        <ShieldAlert size={28} className="text-[var(--muted-foreground)]" />
        <p className="font-mono text-[11px] text-[var(--muted-foreground)]">
          {tMfa("noChallenge")}
        </p>
      </div>
    )
  }

  return (
    <>
      <form
        onSubmit={onSubmit}
        data-testid="as7-mfa-challenge-form"
        onFocus={onFieldFocus}
        className="flex flex-col gap-4 relative"
      >
        <div className="flex flex-col items-center gap-1.5 text-center">
          <AuthBrandWordmark level={level} bloomKey={bloomKey} />
          <Shield size={22} className="text-[var(--artifact-purple)]" />
          <h1 className="font-mono text-base font-semibold text-[var(--foreground)]">
            {tMfa("verifyTitle")}
          </h1>
          <p className="font-mono text-[11px] text-[var(--muted-foreground)] leading-relaxed">
            {(() => {
              const SENTINEL = "__OMNISIGHT_EMAIL_PLACEHOLDER__"
              const body = tMfa("welcome", { email: SENTINEL })
              const [before, after] = body.split(SENTINEL)
              return (
                <>
                  {before}
                  <span className="text-[var(--foreground)]">
                    {auth.mfaPending.email}
                  </span>
                  {after}
                </>
              )
            })()}
          </p>
        </div>

        {offeredMethods.length > 1 ? (
          <MfaMethodTabs
            methods={offeredMethods}
            value={kind}
            onChange={(nextKind) => {
              setKind(nextKind)
              setValue("")
              setPulseKey(bumpPulseKey)
            }}
            disabled={busy || webauthnInFlight || passed}
          />
        ) : null}

        <MethodPanel
          kind={kind}
          level={level}
          value={value}
          pulseKey={pulseKey}
          passed={passed}
          onValueChange={onValueChange}
          onWebauthnClick={onWebauthn}
          busy={busy || passed}
          webauthnInFlight={webauthnInFlight}
        />

        {showError && errorOutcome ? (
          <div
            role="alert"
            data-testid="as7-mfa-error"
            data-as7-mfa-error-kind={errorOutcome.kind}
            className="flex items-start gap-2 p-2 rounded border border-[var(--destructive)] bg-[var(--destructive)]/10 text-[var(--destructive)] font-mono text-xs"
          >
            <AlertCircle size={14} className="shrink-0 mt-0.5" />
            <span>{errorOutcome.message}</span>
          </div>
        ) : null}

        {kind !== MFA_METHOD_KIND.webauthn ? (
          <button
            type="submit"
            data-testid="as7-mfa-submit"
            data-as7-block-reason={submitBlockedReason ?? "ok"}
            disabled={submitDisabled || passed}
            className="flex items-center justify-center gap-2 px-3 py-2 rounded bg-[var(--artifact-purple)] text-white font-mono text-sm font-semibold hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {busy ? <Loader2 size={14} className="animate-spin" /> : null}
            {tMfa("verify")}
          </button>
        ) : null}

        <button
          type="button"
          data-testid="as7-mfa-cancel"
          onClick={onCancel}
          className="flex items-center justify-center gap-1 font-mono text-xs text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
        >
          <ArrowLeft size={12} />
          {tAuth("backToSignIn")}
        </button>

        <p className="font-mono text-[10px] text-[var(--muted-foreground)] text-center leading-relaxed">
          {tMfa("lostAccess")}
        </p>
      </form>

      <MfaPassedCheck
        level={level}
        active={passed}
        onComplete={onPassedComplete}
      />
    </>
  )
}

// ─────────────────────────────────────────────────────────────────
// Page scaffold.
// ─────────────────────────────────────────────────────────────────

function MfaChallengeScaffold() {
  const level = useEffectiveMotionLevel()
  return (
    <AuthVisualFoundation forceLevel={level}>
      <AuthGlassCard level={level}>
        <MfaChallengeForm />
      </AuthGlassCard>
    </AuthVisualFoundation>
  )
}

export default function MfaChallengePage() {
  return <MfaChallengeScaffold />
}
