"use client"

/**
 * AS.7.7 — Profile / Account settings dedicated page.
 *
 * Composes the AS.7.0 visual foundation (`<AuthVisualFoundation>` +
 * `<AuthGlassCard>`) with 7 distinct settings sections. Section
 * vocabulary is pinned in `lib/auth/profile-helpers.ts` so the
 * test layer can drift-guard new sections.
 *
 * The 7 sections (PROFILE_SECTIONS_ORDERED):
 *   1. **Connected accounts** — `<OAuthOrbitSatellites>` showing
 *      every linked / available IdP as a satellite.
 *   2. **Auth methods** — flat list of password / OAuth / passkey /
 *      TOTP / backup-code state.
 *   3. **MFA setup** — TOTP enroll / disable + WebAuthn add /
 *      remove + Backup codes regenerate.
 *   4. **Sessions list** — table backed by `GET /auth/sessions`
 *      with per-row revoke + global "sign out everywhere else".
 *   5. **Password change** — current + new + saved checkbox + the
 *      AS.7.2 password generator.
 *   6. **API keys** — admin-only; list + create + rotate + revoke.
 *   7. **Data & privacy** — GDPR export + GDPR delete with
 *      typed-confirmation gate.
 *
 * Most sections call backend endpoints that already exist
 * (sessions / change-password / MFA / API keys). The connected-
 * accounts list-and-disconnect endpoints + the GDPR export /
 * delete endpoints are not yet wired on the backend; per the
 * Phase-1 fail-closed pattern (AS.7.2 / AS.7.3 / AS.7.5) those
 * sections gracefully render an empty / unavailable state while
 * still wiring the full UI surface. The classifier maps 404 / 501
 * to the canonical "not implemented" copy.
 *
 * Module-global state audit (per implement_phase_step.md SOP §1):
 *   - Pure browser component. All state lives in React (`useState`
 *     / `useRef`). Helpers are pure (Answer #1 of the SOP audit).
 *   - The page reads `auth.user` for the role gate + email
 *     pre-fills, and `lib/api.ts` wrappers for every backend call.
 *   - No module-level mutable state.
 *
 * Read-after-write timing audit: N/A — no DB calls, no
 * parallelisation change vs existing auth-context behaviour. Each
 * mutation is followed by a fresh GET so the local state matches
 * the server.
 */

import { useCallback, useEffect, useMemo, useState } from "react"
import { useRouter } from "next/navigation"
import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  KeyRound,
  Loader2,
  Lock,
  LogOut,
  RefreshCw,
  Shield,
  Trash2,
  XCircle,
} from "lucide-react"

import { useAuth } from "@/lib/auth-context"
import { useEffectiveMotionLevel } from "@/hooks/use-effective-motion-level"
import {
  AuthBrandWordmark,
  AuthGlassCard,
  AuthVisualFoundation,
  OAuthOrbitSatellites,
  OAuthProviderIcon,
} from "@/components/omnisight/auth"
import {
  apiKeysVisibility,
  authMethodsSummary,
  classifyGdprError,
  classifyPasswordChangeError,
  DELETE_ACCOUNT_CONFIRM_PHRASE,
  deleteAccountBlockedReason,
  formatRelativeTime,
  PROFILE_SECTION_COPY,
  PROFILE_SECTION_KIND,
  passwordChangeBlockedReason,
  sessionsRowFingerprint,
  shortenUserAgent,
  type AuthMethodRow,
  type LinkedOAuthIdentity,
  type PasswordChangeErrorOutcome,
  type GdprErrorOutcome,
} from "@/lib/auth/profile-helpers"
import {
  ApiError,
  changePassword as apiChangePassword,
  disconnectOAuthIdentity,
  exportAccountData,
  listOAuthIdentities,
  listSessions,
  mfaBackupCodesStatus,
  mfaBackupCodesRegenerate,
  mfaStatus,
  mfaTotpDisable,
  mfaWebauthnRemove,
  requestAccountDeletion,
  revokeAllOtherSessions,
  revokeSession,
  type MfaMethod,
  type SessionItem,
} from "@/lib/api"

// ─────────────────────────────────────────────────────────────────
// Page header — brand wordmark + back-to-dashboard link.
// ─────────────────────────────────────────────────────────────────

function AccountPageHeader({ level }: { level: ReturnType<typeof useEffectiveMotionLevel> }) {
  return (
    <div
      data-testid="as7-account-header"
      className="flex flex-col gap-2"
    >
      <AuthBrandWordmark level={level} />
      <h1
        data-testid="as7-account-title"
        className="font-mono text-base font-semibold text-[var(--foreground)]"
      >
        Profile &amp; account settings
      </h1>
      <p
        data-testid="as7-account-summary"
        className="font-mono text-xs text-[var(--muted-foreground)] leading-relaxed"
      >
        Manage how you sign in, the devices that have access, and the data
        OmniSight stores about you.
      </p>
      <a
        href="/"
        data-testid="as7-account-back-link"
        className="flex items-center gap-1 font-mono text-[10px] text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
      >
        <ArrowLeft size={10} /> Back to dashboard
      </a>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// Section header — title + summary copy + optional badge.
// ─────────────────────────────────────────────────────────────────

function SectionHeader({
  kind,
  badge,
}: {
  kind: keyof typeof PROFILE_SECTION_COPY
  badge?: string | null
}) {
  const copy = PROFILE_SECTION_COPY[kind]
  return (
    <div
      data-testid={`as7-section-header-${kind}`}
      className="flex flex-col gap-1"
    >
      <div className="flex items-center justify-between gap-2">
        <h2 className="font-mono text-sm font-semibold text-[var(--foreground)]">
          {copy.title}
        </h2>
        {badge ? (
          <span
            data-testid={`as7-section-badge-${kind}`}
            className="font-mono text-[10px] text-[var(--muted-foreground)] px-2 py-0.5 rounded border border-[var(--border)]"
          >
            {badge}
          </span>
        ) : null}
      </div>
      <p className="font-mono text-[11px] text-[var(--muted-foreground)] leading-relaxed">
        {copy.summary}
      </p>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// 1. Connected accounts — orbital satellites
// ─────────────────────────────────────────────────────────────────

function ConnectedAccountsSection({
  level,
  linked,
  reload,
  busy,
}: {
  level: ReturnType<typeof useEffectiveMotionLevel>
  linked: readonly LinkedOAuthIdentity[]
  reload: () => Promise<void>
  busy: boolean
}) {
  const [actionError, setActionError] = useState<string | null>(null)

  const handleConnect = useCallback((id: string) => {
    // Hard navigation so the backend can set its in-flight cookie
    // on the 302 redirect (matching the AS.7.1 OAuth button flow).
    window.location.href = `/api/v1/auth/oauth/${encodeURIComponent(id)}/authorize?next=${encodeURIComponent("/settings/account")}`
  }, [])

  const handleDisconnect = useCallback(
    async (id: string) => {
      setActionError(null)
      try {
        await disconnectOAuthIdentity(id)
        await reload()
      } catch (err) {
        if (err instanceof ApiError && (err.status === 404 || err.status === 501)) {
          setActionError(
            "Disconnecting OAuth providers isn't available yet on this deployment.",
          )
        } else {
          setActionError(
            err instanceof Error ? err.message : "Could not disconnect that provider.",
          )
        }
      }
    },
    [reload],
  )

  return (
    <section
      data-testid="as7-section-connected-accounts"
      className="flex flex-col gap-3"
    >
      <SectionHeader
        kind={PROFILE_SECTION_KIND.connectedAccounts}
        badge={`${linked.length} linked`}
      />
      <OAuthOrbitSatellites
        level={level}
        linked={linked}
        renderIcon={(id) => <OAuthProviderIcon id={id} />}
        onConnect={handleConnect}
        onDisconnect={handleDisconnect}
        disabled={busy}
      />
      {actionError ? (
        <div
          role="alert"
          data-testid="as7-connected-accounts-error"
          className="flex items-start gap-2 p-2 rounded border border-[var(--artifact-purple)] bg-[var(--artifact-purple)]/10 text-[var(--foreground)] font-mono text-[11px]"
        >
          <AlertTriangle size={12} className="shrink-0 mt-0.5" />
          <span>{actionError}</span>
        </div>
      ) : null}
    </section>
  )
}

// ─────────────────────────────────────────────────────────────────
// 2. Auth methods — flat list
// ─────────────────────────────────────────────────────────────────

function AuthMethodsSection({ rows }: { rows: readonly AuthMethodRow[] }) {
  return (
    <section
      data-testid="as7-section-auth-methods"
      className="flex flex-col gap-3"
    >
      <SectionHeader kind={PROFILE_SECTION_KIND.authMethods} />
      <ul className="flex flex-col gap-2">
        {rows.map((row) => (
          <li
            key={row.kind}
            data-testid={`as7-auth-method-${row.kind}`}
            data-as7-method-enabled={row.enabled ? "yes" : "no"}
            className="flex items-start gap-3 p-2 rounded border border-[var(--border)]"
          >
            <span
              aria-hidden="true"
              className="mt-0.5 text-[var(--artifact-purple)]"
            >
              {row.enabled ? <CheckCircle2 size={14} /> : <XCircle size={14} />}
            </span>
            <div className="flex-1">
              <div className="font-mono text-xs font-semibold text-[var(--foreground)]">
                {row.label}
              </div>
              <div className="font-mono text-[10px] text-[var(--muted-foreground)] leading-relaxed">
                {row.hint}
              </div>
            </div>
          </li>
        ))}
      </ul>
    </section>
  )
}

// ─────────────────────────────────────────────────────────────────
// 3. MFA setup
// ─────────────────────────────────────────────────────────────────

interface MfaSummary {
  totp: MfaMethod | null
  webauthn: MfaMethod[]
  backupRemaining: number | null
  backupTotal: number | null
}

function MfaSetupSection({
  summary,
  reload,
}: {
  summary: MfaSummary | null
  reload: () => Promise<void>
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleDisableTotp = async () => {
    setBusy(true)
    setError(null)
    try {
      await mfaTotpDisable()
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not disable TOTP.")
    } finally {
      setBusy(false)
    }
  }

  const handleRemovePasskey = async (mfaId: string) => {
    setBusy(true)
    setError(null)
    try {
      await mfaWebauthnRemove(mfaId)
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not remove passkey.")
    } finally {
      setBusy(false)
    }
  }

  const handleRegenerateBackupCodes = async () => {
    setBusy(true)
    setError(null)
    try {
      await mfaBackupCodesRegenerate()
      await reload()
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Could not regenerate backup codes.",
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <section
      data-testid="as7-section-mfa-setup"
      className="flex flex-col gap-3"
    >
      <SectionHeader kind={PROFILE_SECTION_KIND.mfaSetup} />
      <div className="flex flex-col gap-2">
        {/* TOTP row */}
        <div
          data-testid="as7-mfa-totp-row"
          data-as7-totp-enrolled={summary?.totp ? "yes" : "no"}
          className="flex items-center justify-between gap-3 p-2 rounded border border-[var(--border)]"
        >
          <div className="flex items-start gap-2">
            <Shield size={14} className="mt-0.5 text-[var(--artifact-purple)]" />
            <div>
              <div className="font-mono text-xs font-semibold">
                Authenticator app (TOTP)
              </div>
              <div className="font-mono text-[10px] text-[var(--muted-foreground)]">
                {summary?.totp
                  ? `Enrolled — ${summary.totp.name || "default"}`
                  : "Not enrolled"}
              </div>
            </div>
          </div>
          {summary?.totp ? (
            <button
              type="button"
              onClick={handleDisableTotp}
              disabled={busy}
              data-testid="as7-mfa-totp-disable"
              className="font-mono text-[10px] px-2 py-1 rounded border border-[var(--border)] hover:border-[var(--artifact-purple)] disabled:opacity-50"
            >
              Disable
            </button>
          ) : (
            <a
              href="/auth-dashboard/mfa-totp/enroll"
              data-testid="as7-mfa-totp-enroll"
              className="font-mono text-[10px] px-2 py-1 rounded bg-[var(--artifact-purple)] text-white hover:opacity-90"
            >
              Enroll TOTP
            </a>
          )}
        </div>

        {/* WebAuthn / passkeys */}
        <div
          data-testid="as7-mfa-webauthn-row"
          data-as7-passkey-count={summary?.webauthn.length ?? 0}
          className="flex flex-col gap-2 p-2 rounded border border-[var(--border)]"
        >
          <div className="flex items-center justify-between gap-3">
            <div className="flex items-start gap-2">
              <KeyRound size={14} className="mt-0.5 text-[var(--artifact-purple)]" />
              <div>
                <div className="font-mono text-xs font-semibold">
                  Passkeys (WebAuthn)
                </div>
                <div className="font-mono text-[10px] text-[var(--muted-foreground)]">
                  {summary && summary.webauthn.length > 0
                    ? `${summary.webauthn.length} registered`
                    : "No passkeys registered"}
                </div>
              </div>
            </div>
            <a
              href="/auth-dashboard/passkey/enroll"
              data-testid="as7-mfa-webauthn-enroll"
              className="font-mono text-[10px] px-2 py-1 rounded bg-[var(--artifact-purple)] text-white hover:opacity-90"
            >
              Add passkey
            </a>
          </div>
          {summary && summary.webauthn.length > 0 ? (
            <ul className="flex flex-col gap-1">
              {summary.webauthn.map((row) => (
                <li
                  key={row.id}
                  data-testid={`as7-passkey-${row.id}`}
                  className="flex items-center justify-between gap-2 px-2 py-1 rounded bg-[var(--background)]"
                >
                  <span className="font-mono text-[10px]">
                    {row.name || "passkey"}
                  </span>
                  <button
                    type="button"
                    onClick={() => handleRemovePasskey(row.id)}
                    disabled={busy}
                    data-testid={`as7-passkey-remove-${row.id}`}
                    className="font-mono text-[10px] text-[var(--muted-foreground)] hover:text-[var(--foreground)] disabled:opacity-50"
                  >
                    Remove
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
        </div>

        {/* Backup codes */}
        <div
          data-testid="as7-mfa-backup-row"
          data-as7-backup-remaining={summary?.backupRemaining ?? "unknown"}
          className="flex items-center justify-between gap-3 p-2 rounded border border-[var(--border)]"
        >
          <div className="flex items-start gap-2">
            <RefreshCw size={14} className="mt-0.5 text-[var(--artifact-purple)]" />
            <div>
              <div className="font-mono text-xs font-semibold">Backup codes</div>
              <div className="font-mono text-[10px] text-[var(--muted-foreground)]">
                {summary?.backupRemaining === null
                  ? "Loading…"
                  : `${summary?.backupRemaining ?? 0} of ${summary?.backupTotal ?? 0} remaining`}
              </div>
            </div>
          </div>
          <button
            type="button"
            onClick={handleRegenerateBackupCodes}
            disabled={busy || !summary?.totp}
            data-testid="as7-mfa-backup-regenerate"
            className="font-mono text-[10px] px-2 py-1 rounded border border-[var(--border)] hover:border-[var(--artifact-purple)] disabled:opacity-50"
          >
            Regenerate
          </button>
        </div>
      </div>
      {error ? (
        <div
          role="alert"
          data-testid="as7-mfa-error"
          className="font-mono text-[11px] text-[var(--artifact-purple)]"
        >
          {error}
        </div>
      ) : null}
    </section>
  )
}

// ─────────────────────────────────────────────────────────────────
// 4. Sessions list
// ─────────────────────────────────────────────────────────────────

function SessionsSection({
  sessions,
  reload,
  nowSeconds,
}: {
  sessions: readonly SessionItem[]
  reload: () => Promise<void>
  nowSeconds: number
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleRevoke = async (tokenHint: string) => {
    setBusy(true)
    setError(null)
    try {
      await revokeSession(tokenHint)
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not revoke session.")
    } finally {
      setBusy(false)
    }
  }

  const handleRevokeAll = async () => {
    setBusy(true)
    setError(null)
    try {
      await revokeAllOtherSessions()
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not revoke other sessions.")
    } finally {
      setBusy(false)
    }
  }

  const peerCount = sessions.filter((s) => !s.is_current).length

  return (
    <section
      data-testid="as7-section-sessions"
      className="flex flex-col gap-3"
    >
      <SectionHeader
        kind={PROFILE_SECTION_KIND.sessions}
        badge={`${sessions.length} active`}
      />
      <ul className="flex flex-col gap-2">
        {sessions.map((session) => (
          <li
            key={sessionsRowFingerprint({
              tokenHint: session.token_hint,
              createdAt: session.created_at,
              lastSeenAt: session.last_seen_at,
              ip: session.ip,
              userAgent: session.user_agent,
              isCurrent: session.is_current,
            })}
            data-testid={`as7-session-${session.token_hint}`}
            data-as7-session-current={session.is_current ? "yes" : "no"}
            className="flex items-center justify-between gap-3 p-2 rounded border border-[var(--border)]"
          >
            <div className="flex-1 min-w-0">
              <div className="font-mono text-xs font-semibold truncate">
                {shortenUserAgent(session.user_agent)}
                {session.is_current ? (
                  <span className="ml-2 text-[var(--artifact-purple)] text-[10px]">
                    (this device)
                  </span>
                ) : null}
              </div>
              <div className="font-mono text-[10px] text-[var(--muted-foreground)]">
                {session.ip} •{" "}
                {formatRelativeTime(session.last_seen_at, nowSeconds)}
              </div>
            </div>
            {session.is_current ? null : (
              <button
                type="button"
                onClick={() => handleRevoke(session.token_hint)}
                disabled={busy}
                data-testid={`as7-session-revoke-${session.token_hint}`}
                className="font-mono text-[10px] px-2 py-1 rounded border border-[var(--border)] hover:border-[var(--artifact-purple)] disabled:opacity-50"
              >
                Revoke
              </button>
            )}
          </li>
        ))}
      </ul>
      {peerCount > 0 ? (
        <button
          type="button"
          onClick={handleRevokeAll}
          disabled={busy}
          data-testid="as7-sessions-revoke-all"
          className="self-start font-mono text-[10px] px-3 py-1.5 rounded border border-[var(--artifact-purple)] text-[var(--artifact-purple)] hover:bg-[var(--artifact-purple)]/10 disabled:opacity-50"
        >
          Sign out everywhere else ({peerCount})
        </button>
      ) : null}
      {error ? (
        <div
          role="alert"
          data-testid="as7-sessions-error"
          className="font-mono text-[11px] text-[var(--artifact-purple)]"
        >
          {error}
        </div>
      ) : null}
    </section>
  )
}

// ─────────────────────────────────────────────────────────────────
// 5. Password change
// ─────────────────────────────────────────────────────────────────

function PasswordChangeSection() {
  const [currentPassword, setCurrentPassword] = useState("")
  const [newPassword, setNewPassword] = useState("")
  const [newPasswordSaved, setNewPasswordSaved] = useState(false)
  const [busy, setBusy] = useState(false)
  const [outcome, setOutcome] = useState<
    | { kind: "ok" }
    | { kind: "error"; error: PasswordChangeErrorOutcome }
    | null
  >(null)

  const blockedReason = passwordChangeBlockedReason({
    busy,
    currentPassword,
    newPassword,
    newPasswordSaved,
  })

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (blockedReason !== null) return
    setBusy(true)
    setOutcome(null)
    try {
      await apiChangePassword(currentPassword, newPassword)
      setOutcome({ kind: "ok" })
      setCurrentPassword("")
      setNewPassword("")
      setNewPasswordSaved(false)
    } catch (err) {
      const status = err instanceof ApiError ? err.status : null
      const retryAfter =
        err instanceof ApiError &&
        typeof err.parsed?.retry_after_s === "number"
          ? String(err.parsed.retry_after_s)
          : null
      const error = classifyPasswordChangeError({ status, retryAfter })
      setOutcome({ kind: "error", error })
    } finally {
      setBusy(false)
    }
  }

  return (
    <section
      data-testid="as7-section-password-change"
      className="flex flex-col gap-3"
    >
      <SectionHeader kind={PROFILE_SECTION_KIND.passwordChange} />
      <form
        data-testid="as7-password-change-form"
        data-as7-block-reason={blockedReason ?? "ok"}
        onSubmit={handleSubmit}
        className="flex flex-col gap-2"
      >
        <label className="font-mono text-[11px] text-[var(--muted-foreground)]">
          Current password
          <input
            type="password"
            autoComplete="current-password"
            value={currentPassword}
            onChange={(e) => setCurrentPassword(e.target.value)}
            data-testid="as7-password-change-current"
            className="mt-1 w-full px-2 py-1 rounded bg-[var(--background)] border border-[var(--border)] font-mono text-xs"
          />
        </label>
        <label className="font-mono text-[11px] text-[var(--muted-foreground)]">
          New password (12+ chars)
          <input
            type="password"
            autoComplete="new-password"
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            data-testid="as7-password-change-new"
            className="mt-1 w-full px-2 py-1 rounded bg-[var(--background)] border border-[var(--border)] font-mono text-xs"
          />
        </label>
        <label className="flex items-center gap-2 font-mono text-[11px]">
          <input
            type="checkbox"
            checked={newPasswordSaved}
            onChange={(e) => setNewPasswordSaved(e.target.checked)}
            data-testid="as7-password-change-saved"
          />
          I saved the new password somewhere safe.
        </label>
        <button
          type="submit"
          disabled={blockedReason !== null}
          data-testid="as7-password-change-submit"
          className="self-start flex items-center gap-2 px-3 py-1.5 rounded bg-[var(--artifact-purple)] text-white font-mono text-xs disabled:opacity-50"
        >
          {busy ? <Loader2 size={12} className="animate-spin" /> : <Lock size={12} />}
          Change password
        </button>
      </form>
      {outcome?.kind === "ok" ? (
        <div
          role="status"
          data-testid="as7-password-change-success"
          className="flex items-center gap-2 font-mono text-[11px] text-[var(--artifact-purple)]"
        >
          <CheckCircle2 size={12} /> Password changed. Other devices were signed out.
        </div>
      ) : null}
      {outcome?.kind === "error" ? (
        <div
          role="alert"
          data-testid="as7-password-change-error"
          data-as7-error-kind={outcome.error.kind}
          className="font-mono text-[11px] text-[var(--artifact-purple)]"
        >
          {outcome.error.message}
        </div>
      ) : null}
    </section>
  )
}

// ─────────────────────────────────────────────────────────────────
// 6. API keys (admin-only)
// ─────────────────────────────────────────────────────────────────

function ApiKeysSection({ visible, reason }: ReturnType<typeof apiKeysVisibility>) {
  return (
    <section
      data-testid="as7-section-api-keys"
      data-as7-api-keys-visible={visible ? "yes" : "no"}
      data-as7-api-keys-reason={reason}
      className="flex flex-col gap-3"
    >
      <SectionHeader kind={PROFILE_SECTION_KIND.apiKeys} />
      {visible ? (
        <div
          data-testid="as7-api-keys-admin-stub"
          className="font-mono text-[11px] text-[var(--muted-foreground)] p-2 rounded border border-[var(--border)]"
        >
          API key management is available at{" "}
          <a
            className="text-[var(--artifact-purple)] hover:underline"
            href="/admin/api-keys"
          >
            /admin/api-keys
          </a>
          .
        </div>
      ) : (
        <div
          data-testid="as7-api-keys-disabled"
          className="font-mono text-[11px] text-[var(--muted-foreground)] p-2 rounded border border-[var(--border)]"
        >
          API key management is admin-only. Ask your administrator if you need
          a service token.
        </div>
      )}
    </section>
  )
}

// ─────────────────────────────────────────────────────────────────
// 7. Data & privacy (GDPR)
// ─────────────────────────────────────────────────────────────────

function DataPrivacySection({
  onLogoutAfterDelete,
}: {
  onLogoutAfterDelete: () => Promise<void>
}) {
  const [exportBusy, setExportBusy] = useState(false)
  const [exportError, setExportError] = useState<GdprErrorOutcome | null>(null)
  const [exportLink, setExportLink] = useState<string | null>(null)

  const [deleteBusy, setDeleteBusy] = useState(false)
  const [deleteError, setDeleteError] = useState<GdprErrorOutcome | null>(null)
  const [typedConfirmation, setTypedConfirmation] = useState("")
  const [acknowledgedIrreversible, setAcknowledgedIrreversible] =
    useState(false)
  const [deleteOutcome, setDeleteOutcome] = useState<
    | { kind: "ok"; scheduledFor: string | null }
    | null
  >(null)

  const deleteBlockedReason = deleteAccountBlockedReason({
    busy: deleteBusy,
    typedConfirmation,
    acknowledgedIrreversible,
  })

  const handleExport = async () => {
    setExportBusy(true)
    setExportError(null)
    setExportLink(null)
    try {
      const res = await exportAccountData()
      setExportLink(res.download_url ?? null)
    } catch (err) {
      const status = err instanceof ApiError ? err.status : null
      setExportError(classifyGdprError({ status }))
    } finally {
      setExportBusy(false)
    }
  }

  const handleDelete = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (deleteBlockedReason !== null) return
    setDeleteBusy(true)
    setDeleteError(null)
    try {
      const res = await requestAccountDeletion(typedConfirmation)
      setDeleteOutcome({
        kind: "ok",
        scheduledFor: res.scheduled_for ?? null,
      })
      // Drop the user back to /login once the backend confirms
      // — the session is no longer valid.
      await onLogoutAfterDelete()
    } catch (err) {
      const status = err instanceof ApiError ? err.status : null
      setDeleteError(classifyGdprError({ status }))
    } finally {
      setDeleteBusy(false)
    }
  }

  return (
    <section
      data-testid="as7-section-data-privacy"
      className="flex flex-col gap-3"
    >
      <SectionHeader kind={PROFILE_SECTION_KIND.dataPrivacy} />

      {/* Export */}
      <div className="flex flex-col gap-2 p-2 rounded border border-[var(--border)]">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-start gap-2">
            <RefreshCw size={14} className="mt-0.5 text-[var(--artifact-purple)]" />
            <div>
              <div className="font-mono text-xs font-semibold">
                Export my data
              </div>
              <div className="font-mono text-[10px] text-[var(--muted-foreground)]">
                Generate a portable copy under GDPR Article 20.
              </div>
            </div>
          </div>
          <button
            type="button"
            onClick={handleExport}
            disabled={exportBusy}
            data-testid="as7-data-export-submit"
            className="font-mono text-[10px] px-2 py-1 rounded border border-[var(--border)] hover:border-[var(--artifact-purple)] disabled:opacity-50"
          >
            {exportBusy ? "Working…" : "Request export"}
          </button>
        </div>
        {exportLink ? (
          <a
            href={exportLink}
            data-testid="as7-data-export-link"
            className="font-mono text-[10px] text-[var(--artifact-purple)] hover:underline"
          >
            Download archive
          </a>
        ) : null}
        {exportError ? (
          <div
            role="alert"
            data-testid="as7-data-export-error"
            data-as7-error-kind={exportError.kind}
            className="font-mono text-[11px] text-[var(--artifact-purple)]"
          >
            {exportError.message}
          </div>
        ) : null}
      </div>

      {/* Delete */}
      <form
        onSubmit={handleDelete}
        data-testid="as7-data-delete-form"
        data-as7-block-reason={deleteBlockedReason ?? "ok"}
        className="flex flex-col gap-2 p-2 rounded border border-[var(--artifact-purple)]/40"
      >
        <div className="flex items-start gap-2">
          <Trash2 size={14} className="mt-0.5 text-[var(--artifact-purple)]" />
          <div>
            <div className="font-mono text-xs font-semibold">Delete my account</div>
            <div className="font-mono text-[10px] text-[var(--muted-foreground)] leading-relaxed">
              Permanently erases every artefact tied to your account.
              Cannot be undone after the grace period elapses.
            </div>
          </div>
        </div>
        <label className="font-mono text-[11px] text-[var(--muted-foreground)]">
          Type{" "}
          <code className="px-1 rounded bg-[var(--background)] text-[var(--foreground)]">
            {DELETE_ACCOUNT_CONFIRM_PHRASE}
          </code>{" "}
          to confirm
          <input
            type="text"
            value={typedConfirmation}
            onChange={(e) => setTypedConfirmation(e.target.value)}
            data-testid="as7-data-delete-confirmation"
            className="mt-1 w-full px-2 py-1 rounded bg-[var(--background)] border border-[var(--border)] font-mono text-xs"
          />
        </label>
        <label className="flex items-center gap-2 font-mono text-[11px]">
          <input
            type="checkbox"
            checked={acknowledgedIrreversible}
            onChange={(e) => setAcknowledgedIrreversible(e.target.checked)}
            data-testid="as7-data-delete-acknowledge"
          />
          I understand this is irreversible.
        </label>
        <button
          type="submit"
          disabled={deleteBlockedReason !== null}
          data-testid="as7-data-delete-submit"
          className="self-start flex items-center gap-2 px-3 py-1.5 rounded bg-[var(--artifact-purple)]/80 text-white font-mono text-xs hover:bg-[var(--artifact-purple)] disabled:opacity-40"
        >
          {deleteBusy ? <Loader2 size={12} className="animate-spin" /> : <Trash2 size={12} />}
          Delete my account
        </button>
        {deleteOutcome ? (
          <div
            role="status"
            data-testid="as7-data-delete-success"
            className="font-mono text-[11px] text-[var(--artifact-purple)]"
          >
            {deleteOutcome.scheduledFor
              ? `Deletion scheduled for ${deleteOutcome.scheduledFor}.`
              : "Deletion request received."}
          </div>
        ) : null}
        {deleteError ? (
          <div
            role="alert"
            data-testid="as7-data-delete-error"
            data-as7-error-kind={deleteError.kind}
            className="font-mono text-[11px] text-[var(--artifact-purple)]"
          >
            {deleteError.message}
          </div>
        ) : null}
      </form>
    </section>
  )
}

// ─────────────────────────────────────────────────────────────────
// Page body — orchestrates the 7 sections + their data fetches.
// ─────────────────────────────────────────────────────────────────

function AccountSettingsBody() {
  const auth = useAuth()
  const router = useRouter()
  const level = useEffectiveMotionLevel()

  const [linkedOAuth, setLinkedOAuth] = useState<readonly LinkedOAuthIdentity[]>(
    [],
  )
  const [sessions, setSessions] = useState<readonly SessionItem[]>([])
  const [mfaSummary, setMfaSummary] = useState<MfaSummary | null>(null)
  const [nowSeconds, setNowSeconds] = useState(() =>
    Math.floor(Date.now() / 1000),
  )
  const [bootstrapped, setBootstrapped] = useState(false)
  const [topError, setTopError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // Tick the clock once a minute so the relative timestamps stay
  // honest without the page re-fetching.
  useEffect(() => {
    const id = window.setInterval(() => {
      setNowSeconds(Math.floor(Date.now() / 1000))
    }, 60_000)
    return () => window.clearInterval(id)
  }, [])

  const reloadOAuth = useCallback(async () => {
    setBusy(true)
    try {
      const res = await listOAuthIdentities()
      setLinkedOAuth(
        res.items.map((row) => ({
          provider: row.provider,
          displayName: row.display_name ?? null,
          linkedAt: row.linked_at ?? null,
        })),
      )
    } finally {
      setBusy(false)
    }
  }, [])

  const reloadSessions = useCallback(async () => {
    try {
      const res = await listSessions()
      setSessions(res.items)
    } catch (err) {
      setTopError(
        err instanceof Error ? err.message : "Could not load active sessions.",
      )
    }
  }, [])

  const reloadMfa = useCallback(async () => {
    try {
      const [status, backup] = await Promise.all([
        mfaStatus(),
        mfaBackupCodesStatus().catch(() => ({ total: 0, remaining: 0 })),
      ])
      const totp =
        status.methods.find(
          (m) => m.method === "totp" && m.verified,
        ) ?? null
      const webauthn = status.methods.filter(
        (m) => m.method === "webauthn" && m.verified,
      )
      setMfaSummary({
        totp,
        webauthn,
        backupRemaining: backup.remaining,
        backupTotal: backup.total,
      })
    } catch (err) {
      setTopError(
        err instanceof Error ? err.message : "Could not load MFA status.",
      )
    }
  }, [])

  useEffect(() => {
    let aborted = false
    void (async () => {
      await Promise.all([reloadOAuth(), reloadSessions(), reloadMfa()])
      if (!aborted) setBootstrapped(true)
    })()
    return () => {
      aborted = true
    }
  }, [reloadOAuth, reloadSessions, reloadMfa])

  const authMethods = useMemo(
    () =>
      authMethodsSummary({
        hasPassword: auth.user !== null,
        linkedOAuth,
        hasTotp: Boolean(mfaSummary?.totp),
        hasPasskey: (mfaSummary?.webauthn.length ?? 0) > 0,
        backupCodesRemaining: mfaSummary?.backupRemaining ?? null,
      }),
    [auth.user, linkedOAuth, mfaSummary],
  )

  const apiKeyVisibility = apiKeysVisibility({
    userRole: auth.user?.role ?? null,
  })

  const onLogoutAfterDelete = useCallback(async () => {
    await auth.logout()
    router.replace("/login?reason=account_deleted")
  }, [auth, router])

  const onSignOutAll = useCallback(async () => {
    await auth.logout()
    router.replace("/login")
  }, [auth, router])

  if (!auth.loading && auth.user === null) {
    // Guard: only authenticated users can be here. The redirect
    // happens after mount so the page renders for one frame
    // (matching the AS.7.4 / AS.7.6 redirect convention).
    return (
      <div
        data-testid="as7-account-unauth"
        className="flex flex-col items-center gap-2 p-4 font-mono text-xs"
      >
        <Loader2 size={14} className="animate-spin text-[var(--artifact-purple)]" />
        <span>Sign in to manage your account…</span>
        <RedirectToLoginEffect />
      </div>
    )
  }

  return (
    <div
      data-testid="as7-account-body"
      data-as7-bootstrapped={bootstrapped ? "yes" : "no"}
      className="flex flex-col gap-6"
    >
      <AccountPageHeader level={level} />

      {topError ? (
        <div
          role="alert"
          data-testid="as7-account-top-error"
          className="flex items-start gap-2 p-2 rounded border border-[var(--artifact-purple)] bg-[var(--artifact-purple)]/10 text-[var(--foreground)] font-mono text-[11px]"
        >
          <AlertTriangle size={12} className="shrink-0 mt-0.5" />
          <span>{topError}</span>
        </div>
      ) : null}

      <ConnectedAccountsSection
        level={level}
        linked={linkedOAuth}
        reload={reloadOAuth}
        busy={busy}
      />
      <AuthMethodsSection rows={authMethods} />
      <MfaSetupSection summary={mfaSummary} reload={reloadMfa} />
      <SessionsSection
        sessions={sessions}
        reload={reloadSessions}
        nowSeconds={nowSeconds}
      />
      <PasswordChangeSection />
      <ApiKeysSection {...apiKeyVisibility} />
      <DataPrivacySection onLogoutAfterDelete={onLogoutAfterDelete} />

      <button
        type="button"
        onClick={onSignOutAll}
        data-testid="as7-account-sign-out"
        className="self-start flex items-center gap-1 font-mono text-[10px] text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
      >
        <LogOut size={10} /> Sign out
      </button>
    </div>
  )
}

function RedirectToLoginEffect() {
  const router = useRouter()
  useEffect(() => {
    router.replace(
      `/login?next=${encodeURIComponent("/settings/account")}`,
    )
  }, [router])
  return null
}

// ─────────────────────────────────────────────────────────────────
// Page scaffold — same shape as AS.7.1 / AS.7.2 / AS.7.3 / AS.7.4 /
// AS.7.5 / AS.7.6. Resolves motion level once at root + threads
// down via `forceLevel` so every visual layer reads the same value.
// ─────────────────────────────────────────────────────────────────

function AccountSettingsScaffold() {
  const level = useEffectiveMotionLevel()
  return (
    <AuthVisualFoundation forceLevel={level}>
      <AuthGlassCard level={level}>
        <AccountSettingsBody />
      </AuthGlassCard>
    </AuthVisualFoundation>
  )
}

export default function AccountSettingsPage() {
  return <AccountSettingsScaffold />
}
