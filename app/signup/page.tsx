"use client"

/**
 * AS.7.2 — Signup page redesign.
 *
 * Composes the AS.7.0 visual foundation + AS.7.1 login primitives
 * with three new AS.7.2 surfaces:
 *
 *   - `<PasswordSlotMachine>` — quantum-collapse animation for the
 *     auto-generated password (cycle phase 200ms → collapse 30ms
 *     stagger → settled). Driven by `lib/auth/password-slot-machine
 *     .ts`'s pure reducer.
 *   - `<PasswordStyleToggle>` — Random / Memorable / Pronounceable
 *     3-segment toggle. Each click instantly re-rolls a fresh
 *     password using the AS.0.10 generator.
 *   - `<PasswordStrengthMeter>` — 5-segment strength bar +
 *     HaveIBeenPwned k-anonymity breach lookup.
 *   - `<SaveAcknowledgementCheckbox>` — submit gate that blocks
 *     until the user confirms they've stored the password.
 *
 * Every layer reuses AS.7.0 / AS.7.1: the visual foundation, glass
 * card, brand wordmark, energy spheres for the OAuth alt-path,
 * field-electric inputs for email + tenant, the rotating honeypot,
 * the Turnstile widget, and the warp-drive transition into the
 * post-signup destination (auto-login path) or the email-verify
 * card (verify-required path).
 *
 * Module-global state audit (per implement_phase_step.md SOP §1):
 * pure browser component. All state lives in React `useState` /
 * `useRef`. The auth context owns user / signup-error state. Per-
 * tab / per-worker derivation is trivially identical (Answer #1
 * of the SOP audit).
 *
 * Read-after-write timing audit: N/A — no DB, no parallelisation
 * change vs. existing auth-context behaviour.
 */

import { useEffect, useMemo, useRef, useState } from "react"
import { useRouter, useSearchParams } from "next/navigation"
import {
  AlertCircle,
  ArrowLeft,
  ArrowRight,
  CheckCircle2,
  ChevronDown,
  Copy,
  Dice5,
  Loader2,
  Lock,
  Mail,
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
  OAuthEnergySphere,
  OAuthProviderIcon,
  PASSWORD_STYLE_OPTIONS,
  PasswordSlotMachine,
  PasswordStrengthMeter,
  PasswordStyleToggle,
  SaveAcknowledgementCheckbox,
  WarpDriveTransition,
} from "@/components/omnisight/auth"
import {
  buildOAuthAuthorizeUrl,
  getPrimaryProviders,
  getSecondaryProviders,
  type OAuthProviderId,
} from "@/lib/auth/oauth-providers"
import { bumpShakeKey } from "@/lib/auth/login-form-helpers"
import {
  signupHoneypotFieldName,
  signupSubmitBlockedReason,
} from "@/lib/auth/signup-form-helpers"
import {
  generate,
  type GeneratedPassword,
  type PasswordStyle,
} from "@/templates/_shared/password-generator"
import type { StrengthResult } from "@/lib/password_strength"

const TURNSTILE_SITE_KEY: string | null =
  process.env.NEXT_PUBLIC_TURNSTILE_SITE_KEY ?? null

// ─────────────────────────────────────────────────────────────────
// Email-verification terminal card (post-submit, verify-required)
// ─────────────────────────────────────────────────────────────────

function EmailVerifyCard({ email }: { email: string }) {
  return (
    <div
      data-testid="as7-signup-verify-card"
      className="flex flex-col gap-4 items-center text-center"
    >
      <Mail size={32} className="text-[var(--artifact-purple)]" />
      <h1 className="font-mono text-base font-semibold text-[var(--foreground)]">
        Check your inbox
      </h1>
      <p className="font-mono text-[12px] text-[var(--muted-foreground)] leading-relaxed">
        We sent a verification link to{" "}
        <span className="text-[var(--foreground)]">{email}</span>. Click
        the link in the email to activate your account, then return
        here to sign in.
      </p>
      <a
        href="/login"
        className="flex items-center gap-2 px-3 py-2 rounded bg-[var(--artifact-purple)] text-white font-mono text-xs font-semibold hover:opacity-90"
      >
        Back to sign in <ArrowRight size={14} />
      </a>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// Signup form (default path)
// ─────────────────────────────────────────────────────────────────

function SignupForm() {
  const router = useRouter()
  const search = useSearchParams()
  const next = search.get("next") || "/"
  const auth = useAuth()
  const level = useEffectiveMotionLevel()

  const [email, setEmail] = useState("")
  const [password, setPassword] = useState("")
  const [style, setStyle] = useState<PasswordStyle>("random")
  const [generated, setGenerated] = useState<GeneratedPassword | null>(null)
  const [animationKey, setAnimationKey] = useState(0)
  const [hasSaved, setHasSaved] = useState(false)
  const [hasAcceptedTos, setHasAcceptedTos] = useState(false)
  const [busy, setBusy] = useState(false)
  const [errorKey, setErrorKey] = useState(0)
  const [warpActive, setWarpActive] = useState(false)
  const [showSecondary, setShowSecondary] = useState(false)
  const [bloomKey, setBloomKey] = useState(0)
  const [turnstileToken, setTurnstileToken] = useState<string | null>(null)
  const [strength, setStrength] = useState<StrengthResult | null>(null)
  const [verifyEmail, setVerifyEmail] = useState<string | null>(null)
  const [showPassword, setShowPassword] = useState(false)
  const [copied, setCopied] = useState(false)
  const honeypotFieldRef = useRef<string | null>(null)
  const honeypotResolvedRef = useRef<boolean>(false)
  const [, setHoneypotTick] = useState(0)
  const honeypotResolved = honeypotResolvedRef.current

  // Auto-redirect once signed in (auth-context absorbed the user).
  useEffect(() => {
    if (!auth.loading && auth.user && !verifyEmail) router.replace(next)
  }, [auth.loading, auth.user, next, router, verifyEmail])

  // Pre-fetch the rotating honeypot field name so the AS.7.2 form
  // path's `sg_*` prefix lands in `honeypotFieldRef` before the
  // user can submit. The page's `<AuthHoneypotField>` also fires
  // its own resolver but we own the rendering for the signup form
  // path here so the gate below is reliable.
  useEffect(() => {
    let cancelled = false
    void signupHoneypotFieldName()
      .then((name) => {
        if (cancelled) return
        honeypotFieldRef.current = name
        honeypotResolvedRef.current = true
        setHoneypotTick((t) => t + 1)
      })
      .catch(() => {
        // Web Crypto unavailable. Leave resolved=false; the gate
        // will keep submit disabled.
      })
    return () => {
      cancelled = true
    }
  }, [])

  // ── Auto-generation on style change / initial mount ──────────
  // The signup design pre-fills a strong password as soon as the
  // page mounts so the keychain prompt fires + the user has
  // something they can immediately copy. Switching style instantly
  // re-rolls so the toggle feels alive.
  const rollPassword = (nextStyle: PasswordStyle = style) => {
    const out = generate(nextStyle)
    setGenerated(out)
    setPassword(out.password)
    setHasSaved(false)  // any re-roll clears the "saved" ack
    setAnimationKey((k) => k + 1)
    setCopied(false)
  }

  // First mount — roll a password.
  useEffect(() => {
    rollPassword("random")
    // We deliberately do NOT depend on `style` here; user-driven
    // style toggles fire `rollPassword(nextStyle)` directly.
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
      // Clipboard permission denied — silently swallow; the user
      // can still select-copy from the visible field.
    }
  }

  const primaryProviders = useMemo(getPrimaryProviders, [])
  const secondaryProviders = useMemo(getSecondaryProviders, [])

  const onFieldFocus = () => setBloomKey((k) => k + 1)

  const submitBlockedReason = signupSubmitBlockedReason({
    email,
    password,
    passwordPasses: strength?.passes ?? false,
    hasSaved,
    hasAcceptedTos,
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
      const outcome = await auth.signup(
        {
          email,
          password,
          tos_accepted_at: new Date().toISOString(),
        },
        extras,
      )
      if (outcome.status === "ok") {
        setWarpActive(true)
      } else if (outcome.status === "verifyEmail") {
        setVerifyEmail(outcome.email)
      } else {
        setErrorKey(bumpShakeKey)
      }
    } finally {
      setBusy(false)
    }
  }

  const onWarpComplete = () => {
    router.replace(next)
  }

  if (verifyEmail) {
    return <EmailVerifyCard email={verifyEmail} />
  }

  const errorOutcome = auth.lastSignupError
  const hasError = Boolean(errorOutcome)
  const submitDisabled = submitBlockedReason !== null

  return (
    <>
      <form
        onSubmit={onSubmit}
        data-testid="as7-signup-form"
        className="flex flex-col gap-4 relative"
      >
        <div className="flex flex-col items-center gap-1.5 text-center">
          <AuthBrandWordmark level={level} bloomKey={bloomKey} />
          <p className="font-mono text-[11px] text-[var(--muted-foreground)]">
            Create a new OmniSight account
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

        {/* ─── Password slot machine block ───────────────────── */}
        <div
          data-testid="as7-signup-password-block"
          className="flex flex-col gap-2 rounded border border-[var(--border)] bg-[var(--background)]/30 p-3"
        >
          <div className="flex items-center justify-between gap-2">
            <span className="font-mono text-[10px] tracking-wider text-[var(--muted-foreground)] flex items-center gap-1">
              <Sparkles size={10} className="text-[var(--artifact-purple)]" />
              AUTO-GENERATED PASSWORD
            </span>
            <div className="flex items-center gap-1">
              <button
                type="button"
                data-testid="as7-signup-reroll"
                onClick={() => rollPassword(style)}
                title="Roll a fresh password"
                aria-label="Roll a fresh password"
                className="p-1 rounded text-[var(--muted-foreground)] hover:text-[var(--foreground)] hover:bg-[var(--background)]/60"
              >
                <Dice5 size={14} />
              </button>
              <button
                type="button"
                data-testid="as7-signup-copy"
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
                data-testid="as7-signup-toggle-show"
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
              data-testid="as7-signup-password-mask"
              className="font-mono text-sm tracking-widest text-[var(--foreground)] py-1"
            >
              {password ? "•".repeat(Math.min(password.length, 32)) : ""}
            </div>
          )}

          <PasswordStyleToggle value={style} onChange={onStyleChange} />

          {/* Hidden text input so password managers / browser
              keychain prompts pick the value up. The visible UI
              above is decorative; submit reads from `password`
              state which is kept in sync with the generated value
              (or the user's typed override if they edit this
              field). */}
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

        <label
          data-testid="as7-signup-tos"
          className="flex items-start gap-2 cursor-pointer select-none p-2 rounded border border-[var(--border)] font-mono text-[11px] leading-relaxed text-[var(--muted-foreground)] hover:text-[var(--foreground)] transition-colors"
        >
          <input
            type="checkbox"
            checked={hasAcceptedTos}
            onChange={(e) => setHasAcceptedTos(e.target.checked)}
            data-testid="as7-signup-tos-input"
            className="mt-0.5 accent-[var(--artifact-purple)]"
          />
          <span>
            I agree to the OmniSight{" "}
            <a
              href="/legal/terms"
              className="underline hover:text-[var(--foreground)]"
              target="_blank"
              rel="noopener noreferrer"
            >
              Terms of Service
            </a>{" "}
            and{" "}
            <a
              href="/legal/privacy"
              className="underline hover:text-[var(--foreground)]"
              target="_blank"
              rel="noopener noreferrer"
            >
              Privacy Policy
            </a>
            .
          </span>
        </label>

        <AuthHoneypotField
          onResolved={(name) => {
            // Surface for the page-level submit gate even though
            // the `useEffect` resolver above also tracks it. Both
            // converge on the same name (deterministic).
            honeypotFieldRef.current = name
            honeypotResolvedRef.current = true
            setHoneypotTick((t) => t + 1)
          }}
        />

        {TURNSTILE_SITE_KEY ? (
          <div className="flex justify-center">
            <AuthTurnstileWidget
              siteKey={TURNSTILE_SITE_KEY}
              action="signup"
              onToken={(token) => setTurnstileToken(token)}
              onExpired={() => setTurnstileToken(null)}
              onError={() => setTurnstileToken(null)}
            />
          </div>
        ) : null}

        {hasError && errorOutcome ? (
          <div
            role="alert"
            data-testid="as7-signup-error"
            className="flex items-start gap-2 p-2 rounded border border-[var(--destructive)] bg-[var(--destructive)]/10 text-[var(--destructive)] font-mono text-xs"
          >
            <AlertCircle size={14} className="shrink-0 mt-0.5" />
            <span>{errorOutcome.message}</span>
          </div>
        ) : null}

        <button
          type="submit"
          data-testid="as7-signup-submit"
          disabled={submitDisabled}
          data-as7-block-reason={submitBlockedReason ?? "ok"}
          className="flex items-center justify-center gap-2 px-3 py-2 rounded bg-[var(--artifact-purple)] text-white font-mono text-sm font-semibold hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {busy ? <Loader2 size={14} className="animate-spin" /> : null}
          Create account
        </button>

        {/* OAuth alt-path — exact same surface as login. Users who
            don't want a password can pivot to GitHub / Google /
            etc. and the backend OAuth callback creates the account
            on first login. */}
        <div className="flex flex-col gap-3 mt-1">
          <div className="flex items-center gap-2 text-[var(--muted-foreground)] font-mono text-[10px] tracking-wider">
            <span className="flex-1 h-px bg-[var(--border)]" />
            OR CONTINUE WITH
            <span className="flex-1 h-px bg-[var(--border)]" />
          </div>
          <div
            data-testid="as7-signup-oauth-row-primary"
            className="flex items-center justify-center gap-3 flex-wrap"
          >
            {primaryProviders.map((p) => (
              <OAuthEnergySphere
                key={p.id}
                level={level}
                provider={p}
                href={buildOAuthAuthorizeUrl(p.id, next)}
                icon={<OAuthProviderIcon id={p.id as OAuthProviderId} />}
              />
            ))}
          </div>
          <button
            type="button"
            data-testid="as7-signup-oauth-more-toggle"
            onClick={() => setShowSecondary((v) => !v)}
            className="self-center flex items-center gap-1 px-2 py-1 rounded font-mono text-[11px] text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
            aria-expanded={showSecondary}
          >
            <ChevronDown
              size={12}
              style={{
                transform: showSecondary ? "rotate(180deg)" : "rotate(0deg)",
                transition: "transform 180ms ease",
              }}
            />
            {showSecondary ? "Hide more" : "More providers"}
          </button>
          {showSecondary && (
            <div
              data-testid="as7-signup-oauth-row-secondary"
              className="flex items-center justify-center gap-2 flex-wrap"
            >
              {secondaryProviders.map((p) => (
                <OAuthEnergySphere
                  key={p.id}
                  level={level}
                  provider={p}
                  href={buildOAuthAuthorizeUrl(p.id, next)}
                  icon={<OAuthProviderIcon id={p.id as OAuthProviderId} />}
                  size="secondary"
                />
              ))}
            </div>
          )}
        </div>

        <p className="font-mono text-[10px] text-[var(--muted-foreground)] text-center leading-relaxed">
          Already have an account?{" "}
          <a
            href="/login"
            className="underline hover:text-[var(--foreground)] inline-flex items-center gap-1"
          >
            <ArrowLeft size={10} /> Sign in
          </a>
        </p>

        {/* Style hint — small footer reading the active style's
            entropy estimate so the user knows roughly how strong
            the auto-gen output is. */}
        {generated ? (
          <p
            data-testid="as7-signup-entropy-hint"
            className="font-mono text-[10px] text-[var(--muted-foreground)] text-center"
          >
            Style:{" "}
            {PASSWORD_STYLE_OPTIONS.find((o) => o.id === style)?.label}
            {" — "}≈{generated.entropyBits} bits of entropy across{" "}
            {generated.length} chars
          </p>
        ) : null}
      </form>

      <WarpDriveTransition
        level={level}
        active={warpActive}
        onComplete={onWarpComplete}
      />
    </>
  )
}

// ─────────────────────────────────────────────────────────────────
// Page scaffold — same shape as AS.7.1 LoginScaffold
// ─────────────────────────────────────────────────────────────────

function SignupScaffold() {
  const level = useEffectiveMotionLevel()
  return (
    <AuthVisualFoundation forceLevel={level}>
      <AuthGlassCard level={level}>
        <SignupForm />
      </AuthGlassCard>
    </AuthVisualFoundation>
  )
}

export default function SignupPage() {
  return <SignupScaffold />
}
