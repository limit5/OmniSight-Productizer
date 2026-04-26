"use client"

/**
 * Y8 row 7 — invite-accept page at `/invite/<invite_id>.<token>`.
 *
 * Single path segment encodes both the invite id and the plaintext
 * token, joined with `.`. The split is unambiguous because:
 *   • invite_id matches `^inv-[a-z0-9]{4,64}$` (no dots)
 *   • token is `secrets.token_urlsafe(32)` ≈ url-safe base64 (chars
 *     `[A-Za-z0-9_-]`, no dots)
 * so the first `.` is always the delimiter between the two parts.
 *
 * The backend accept endpoint (POST /api/v1/invites/{id}/accept) is
 * intentionally open to anonymous AND authenticated callers. This
 * page picks the branch off useAuth():
 *
 *   • logged-in operator → simple Accept button, body just carries
 *     {token}; backend verifies session.email matches invite.email
 *     or returns 409 with both addresses for inline display.
 *   • anonymous visitor → name+password form; backend creates the
 *     user row on first accept (or upserts membership onto an
 *     existing account if one was found by lower(email)).
 *
 * Module-global state audit
 * ─────────────────────────
 * None introduced. The page reads `useAuth()` (per-tab React
 * context already audited in row 1) and calls `acceptInvite()` —
 * a fire-and-await wrapper that does NOT mutate any module-globals
 * (no X-Tenant-Id is set by accept; the server resolves the tenant
 * from the invite row). Visiting `/invite/...` while already inside
 * a tenant context does not change the dashboard's selected tenant.
 *
 * Read-after-write timing audit
 * ─────────────────────────────
 * N/A. Single sequential `await acceptInvite(...)`, then either
 * redirect or render success/error placard from the response body.
 * No shared state; no in-flight reads; nothing to race.
 */

import { use, useCallback, useMemo, useState } from "react"
import { useRouter } from "next/navigation"
import Link from "next/link"
import {
  AlertCircle,
  ArrowLeft,
  CheckCircle2,
  CircleAlert,
  Clock,
  Loader2,
  Lock,
  LogIn,
  Mail,
  ShieldAlert,
  ShieldCheck,
  UserPlus,
} from "lucide-react"
import {
  ApiError,
  acceptInvite,
  type AcceptInviteResponse,
} from "@/lib/api"
import { useAuth } from "@/lib/auth-context"

// Mirrors backend INVITE_ID_PATTERN (tenant_invites.py).
const INVITE_ID_PATTERN = /^inv-[a-z0-9]{4,64}$/
// Light token-shape sanity check. Backend validates length 16..512;
// we guard the floor here so a malformed link short-circuits before
// burning the per-invite rate-limit bucket on the accept endpoint.
const TOKEN_MIN_CHARS = 16
const TOKEN_MAX_CHARS = 512
// secrets.token_urlsafe(32) → ~43 chars of url-safe base64 alphabet.
// Anything outside this character class is malformed at the URL
// layer (browser would have refused the navigation anyway).
const TOKEN_CHARSET_RE = /^[A-Za-z0-9_-]+$/

interface ParsedInviteUrl {
  inviteId: string
  token: string
}

interface ParseError {
  reason: "missing_separator" | "bad_invite_id" | "bad_token"
  detail: string
}

function parseInviteUrlSegment(
  segment: string,
): ParsedInviteUrl | ParseError {
  const decoded = (() => {
    try {
      return decodeURIComponent(segment)
    } catch {
      return segment
    }
  })()
  const sepIdx = decoded.indexOf(".")
  if (sepIdx <= 0 || sepIdx === decoded.length - 1) {
    return {
      reason: "missing_separator",
      detail: "Expected `<invite_id>.<token>`; no `.` separator found.",
    }
  }
  const inviteId = decoded.slice(0, sepIdx)
  const token = decoded.slice(sepIdx + 1)
  if (!INVITE_ID_PATTERN.test(inviteId)) {
    return {
      reason: "bad_invite_id",
      detail: `Invite id "${inviteId}" does not match expected shape (^inv-[a-z0-9]{4,64}$).`,
    }
  }
  if (
    token.length < TOKEN_MIN_CHARS
    || token.length > TOKEN_MAX_CHARS
    || !TOKEN_CHARSET_RE.test(token)
  ) {
    return {
      reason: "bad_token",
      detail: "Token segment is malformed (length or character set).",
    }
  }
  return { inviteId, token }
}

interface AcceptError {
  status: number
  detail: string
  parsed: Record<string, unknown> | null
  retryAfterSeconds: number | null
}

function describeAcceptError(exc: unknown): AcceptError {
  if (exc instanceof ApiError) {
    const parsed = exc.parsed as Record<string, unknown> | null
    const detail = (parsed && typeof parsed.detail === "string")
      ? parsed.detail
      : exc.body || `HTTP ${exc.status}`
    let retry: number | null = null
    if (parsed && typeof parsed.retry_after_seconds === "number") {
      retry = parsed.retry_after_seconds
    }
    return { status: exc.status, detail, parsed, retryAfterSeconds: retry }
  }
  return {
    status: 0,
    detail: exc instanceof Error ? exc.message : String(exc),
    parsed: null,
    retryAfterSeconds: null,
  }
}

export default function InviteAcceptPage({
  params,
}: {
  params: Promise<{ token: string }>
}) {
  const { token: pathSegment } = use(params)
  const router = useRouter()
  const { user, authMode, loading: authLoading, logout } = useAuth()

  const parsed = useMemo(
    () => parseInviteUrlSegment(pathSegment),
    [pathSegment],
  )

  // Anon-branch form state (only consulted when there's no session).
  const [formName, setFormName] = useState("")
  const [formPassword, setFormPassword] = useState("")
  const [busy, setBusy] = useState(false)
  const [acceptError, setAcceptError] = useState<AcceptError | null>(null)
  const [success, setSuccess] = useState<AcceptInviteResponse | null>(null)

  const isLoggedIn = useMemo(
    () => Boolean(user && (authMode === "session" || authMode === "strict")),
    [user, authMode],
  )

  const canSubmit = "inviteId" in parsed && !busy && !success

  const onSubmitAuthed = useCallback(async () => {
    if (!("inviteId" in parsed) || busy || success) return
    setBusy(true)
    setAcceptError(null)
    try {
      const out = await acceptInvite(parsed.inviteId, { token: parsed.token })
      setSuccess(out)
    } catch (exc) {
      setAcceptError(describeAcceptError(exc))
    } finally {
      setBusy(false)
    }
  }, [parsed, busy, success])

  const onSubmitAnon = useCallback(async (ev: React.FormEvent) => {
    ev.preventDefault()
    if (!("inviteId" in parsed) || busy || success) return
    setBusy(true)
    setAcceptError(null)
    try {
      const out = await acceptInvite(parsed.inviteId, {
        token: parsed.token,
        name: formName.trim().slice(0, 160),
        password: formPassword || null,
      })
      setSuccess(out)
    } catch (exc) {
      setAcceptError(describeAcceptError(exc))
    } finally {
      setBusy(false)
    }
  }, [parsed, busy, success, formName, formPassword])

  const onSignOutAndRetry = useCallback(async () => {
    await logout()
    setAcceptError(null)
  }, [logout])

  // ─── render branches ───────────────────────────────────────────

  // Auth still loading — render a spinner placeholder so we don't
  // briefly show the anon-form before the session is known.
  if (authLoading) {
    return (
      <main
        className="min-h-screen flex items-center justify-center bg-[var(--background)] text-[var(--foreground)]"
        data-testid="invite-loading"
      >
        <div className="font-mono text-xs text-[var(--muted-foreground)] flex items-center gap-2">
          <Loader2 size={14} className="animate-spin" />
          Verifying session…
        </div>
      </main>
    )
  }

  if (!("inviteId" in parsed)) {
    return (
      <main
        className="min-h-screen flex items-center justify-center bg-[var(--background)] text-[var(--foreground)] p-6"
        data-testid="invite-bad-link"
      >
        <div className="max-w-md w-full rounded border border-[var(--destructive)]/40 bg-[var(--card)] p-6 font-mono">
          <div className="flex items-center gap-2 text-[var(--destructive)] mb-2">
            <CircleAlert size={16} />
            <span className="text-sm font-semibold">Invalid invite link</span>
          </div>
          <p className="text-xs text-[var(--muted-foreground)] leading-relaxed mb-1">
            {parsed.detail}
          </p>
          <p className="text-xs text-[var(--muted-foreground)] leading-relaxed mb-4">
            Ask the admin to re-send the invite email — the link should
            look like{" "}
            <code className="px-1 rounded bg-[var(--secondary)]/40">
              /invite/inv-…&hairsp;.&hairsp;…token…
            </code>
            .
          </p>
          <Link
            href="/"
            className="inline-flex items-center gap-1 text-xs underline text-[var(--neural-blue)]"
          >
            <ArrowLeft size={12} /> Back to dashboard
          </Link>
        </div>
      </main>
    )
  }

  // ─── happy path: form / button + status surface ────────────────
  return (
    <main
      className="min-h-screen flex items-center justify-center bg-[var(--background)] text-[var(--foreground)] p-6"
      data-testid="invite-page"
    >
      <div className="max-w-md w-full rounded-lg border border-[var(--border)] bg-[var(--card)] p-6 font-mono flex flex-col gap-4">
        <header className="flex items-center gap-2">
          <Mail size={18} className="text-[var(--neural-blue)]" />
          <h1 className="text-base font-semibold">Tenant invitation</h1>
        </header>
        <p className="text-xs text-[var(--muted-foreground)] leading-relaxed">
          You&apos;ve received a one-time invite to join a tenant. Tenant id,
          role, and email address are bound to the token below — the
          server resolves them on accept.
        </p>
        <div
          className="text-[10px] text-[var(--muted-foreground)] flex flex-col gap-0.5"
          data-testid="invite-meta"
        >
          <div>
            <span className="opacity-60">invite_id:</span>{" "}
            <code
              className="px-1 rounded bg-[var(--secondary)]/40"
              data-testid="invite-meta-id"
            >
              {parsed.inviteId}
            </code>
          </div>
          <div>
            <span className="opacity-60">token:</span>{" "}
            <code className="px-1 rounded bg-[var(--secondary)]/40">
              {parsed.token.slice(0, 6)}…{parsed.token.slice(-4)}
            </code>
            <span className="opacity-60"> ({parsed.token.length} chars)</span>
          </div>
        </div>

        {success && (
          <SuccessPanel
            outcome={success}
            isLoggedIn={isLoggedIn}
            onContinueDashboard={() => router.replace("/")}
          />
        )}

        {!success && acceptError && (
          <ErrorPanel
            err={acceptError}
            isLoggedIn={isLoggedIn}
            onSignOut={onSignOutAndRetry}
          />
        )}

        {!success && isLoggedIn && (
          <div
            className="flex flex-col gap-3"
            data-testid="invite-authed-panel"
          >
            <div className="text-xs text-[var(--muted-foreground)] flex items-center gap-1.5">
              <ShieldCheck size={14} className="text-[var(--neural-blue)]" />
              Signed in as{" "}
              <code
                className="px-1 rounded bg-[var(--secondary)]/40"
                data-testid="invite-authed-email"
              >
                {user!.email}
              </code>
            </div>
            <button
              type="button"
              data-testid="invite-accept-btn"
              disabled={!canSubmit}
              onClick={onSubmitAuthed}
              className="inline-flex items-center justify-center gap-2 rounded border border-[var(--neural-blue)] bg-[var(--neural-blue)]/10 hover:bg-[var(--neural-blue)]/20 disabled:opacity-50 disabled:cursor-not-allowed text-xs font-semibold text-[var(--neural-blue)] py-2"
            >
              {busy
                ? <><Loader2 size={14} className="animate-spin" /> Accepting…</>
                : <>Accept invitation</>}
            </button>
            <p className="text-[10px] text-[var(--muted-foreground)] leading-relaxed">
              Membership will be added to your account immediately. The
              invite is single-use and will be marked accepted on success.
            </p>
          </div>
        )}

        {!success && !isLoggedIn && (
          <form
            onSubmit={onSubmitAnon}
            className="flex flex-col gap-3"
            data-testid="invite-anon-form"
          >
            <div className="text-xs text-[var(--muted-foreground)] flex items-center gap-1.5">
              <UserPlus size={14} className="text-[var(--neural-blue)]" />
              Create an account or claim the invite anonymously.
            </div>
            <label className="flex flex-col gap-1">
              <span className="text-[10px] tracking-wider opacity-60">
                YOUR NAME (optional)
              </span>
              <input
                type="text"
                data-testid="invite-name-input"
                value={formName}
                maxLength={160}
                onChange={(ev) => setFormName(ev.target.value)}
                placeholder="Display name shown to your team"
                className="px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--background)] text-xs text-[var(--foreground)] outline-none focus:ring-1 focus:ring-[var(--neural-blue)]"
                autoComplete="name"
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-[10px] tracking-wider opacity-60">
                PASSWORD (optional — set later if blank)
              </span>
              <div className="flex items-center gap-2 px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--background)] focus-within:ring-1 focus-within:ring-[var(--neural-blue)]">
                <Lock size={12} className="opacity-50" />
                <input
                  type="password"
                  data-testid="invite-password-input"
                  value={formPassword}
                  onChange={(ev) => setFormPassword(ev.target.value)}
                  placeholder="••••••••"
                  className="flex-1 bg-transparent outline-none text-xs"
                  autoComplete="new-password"
                />
              </div>
            </label>
            <button
              type="submit"
              data-testid="invite-anon-submit"
              disabled={!canSubmit}
              className="inline-flex items-center justify-center gap-2 rounded border border-[var(--neural-blue)] bg-[var(--neural-blue)]/10 hover:bg-[var(--neural-blue)]/20 disabled:opacity-50 disabled:cursor-not-allowed text-xs font-semibold text-[var(--neural-blue)] py-2"
            >
              {busy
                ? <><Loader2 size={14} className="animate-spin" /> Accepting…</>
                : <>Accept &amp; create account</>}
            </button>
            <div className="flex items-center justify-between pt-1 border-t border-[var(--border)] mt-1">
              <span className="text-[10px] opacity-60">
                Already have an account?
              </span>
              <Link
                href={`/login?next=${encodeURIComponent(`/invite/${pathSegment}`)}`}
                data-testid="invite-login-link"
                className="text-[10px] inline-flex items-center gap-1 underline text-[var(--neural-blue)]"
              >
                <LogIn size={10} /> Sign in &amp; come back
              </Link>
            </div>
          </form>
        )}
      </div>
    </main>
  )
}

function SuccessPanel({
  outcome,
  isLoggedIn,
  onContinueDashboard,
}: {
  outcome: AcceptInviteResponse
  isLoggedIn: boolean
  onContinueDashboard: () => void
}) {
  const headline = outcome.already_member
    ? "You're already a member — nothing to do."
    : isLoggedIn
      ? `Joined ${outcome.tenant_id} as ${outcome.role}.`
      : outcome.user_was_created
        ? `Account created. Joined ${outcome.tenant_id} as ${outcome.role}.`
        : `Membership added to ${outcome.user_email}. Joined ${outcome.tenant_id} as ${outcome.role}.`

  return (
    <div
      className="rounded border border-[var(--neural-blue)]/40 bg-[var(--neural-blue)]/10 p-3 flex flex-col gap-2"
      data-testid="invite-success-panel"
    >
      <div className="text-xs font-semibold text-[var(--neural-blue)] inline-flex items-center gap-1.5">
        <CheckCircle2 size={14} /> Invitation accepted
      </div>
      <p
        className="text-[11px] text-[var(--foreground)] leading-relaxed"
        data-testid="invite-success-headline"
      >
        {headline}
      </p>
      {isLoggedIn && (
        <button
          type="button"
          data-testid="invite-go-dashboard"
          onClick={onContinueDashboard}
          className="self-start text-[10px] underline text-[var(--neural-blue)]"
        >
          → Continue to dashboard
        </button>
      )}
      {!isLoggedIn && (
        <Link
          href="/login"
          data-testid="invite-go-login"
          className="self-start text-[10px] underline text-[var(--neural-blue)] inline-flex items-center gap-1"
        >
          <LogIn size={10} /> Sign in to use your new membership
        </Link>
      )}
    </div>
  )
}

function ErrorPanel({
  err,
  isLoggedIn,
  onSignOut,
}: {
  err: AcceptError
  isLoggedIn: boolean
  onSignOut: () => void
}) {
  // Map status → testid + headline so tests can assert the user-
  // visible state without coupling to the wording.
  let testid = "invite-error-generic"
  let icon = <AlertCircle size={14} />
  let headline = "Could not accept the invitation."
  let body: React.ReactNode = err.detail
  let actions: React.ReactNode = null

  const parsed = err.parsed ?? {}
  const status = err.status

  if (status === 404) {
    testid = "invite-error-not-found"
    icon = <CircleAlert size={14} />
    headline = "Invitation not found."
    body = "Double-check the link from your email — the invite may have been deleted by an admin, or the URL was truncated."
  } else if (status === 403) {
    testid = "invite-error-bad-token"
    icon = <ShieldAlert size={14} />
    headline = "Token does not match."
    body = "The token in the URL was rejected by the server. Re-open the link from your email rather than typing it by hand."
  } else if (status === 410) {
    testid = "invite-error-expired"
    icon = <Clock size={14} />
    headline = "Invitation has expired."
    body = "Ask the admin to issue a fresh invite — invites are valid for 7 days from creation."
  } else if (status === 409) {
    const inviteEmail = (parsed as { invite_email?: string }).invite_email
    const sessionEmail = (parsed as { session_email?: string }).session_email
    if (inviteEmail && sessionEmail) {
      testid = "invite-error-email-mismatch"
      icon = <ShieldAlert size={14} />
      headline = "This invite is for a different email."
      body = (
        <span>
          The invite was issued to{" "}
          <code className="px-1 rounded bg-[var(--secondary)]/40" data-testid="invite-error-invite-email">
            {inviteEmail}
          </code>
          , but you are signed in as{" "}
          <code className="px-1 rounded bg-[var(--secondary)]/40" data-testid="invite-error-session-email">
            {sessionEmail}
          </code>
          . Sign out and re-open the link, or ask the admin to re-issue it to your address.
        </span>
      )
      actions = isLoggedIn ? (
        <button
          type="button"
          data-testid="invite-signout-retry"
          onClick={onSignOut}
          className="self-start text-[10px] underline text-[var(--neural-blue)]"
        >
          Sign out and try again
        </button>
      ) : null
    } else {
      const currentStatus = (parsed as { current_status?: string }).current_status
      if (currentStatus === "accepted") {
        testid = "invite-error-already-accepted"
        headline = "Invitation already accepted."
        body = "This invite has been used. Sign in with the account it was issued to."
      } else if (currentStatus === "revoked") {
        testid = "invite-error-revoked"
        headline = "Invitation revoked."
        body = "An admin revoked this invite. Ask them to issue a fresh one if you still need access."
      } else {
        testid = "invite-error-conflict"
        headline = "Invitation cannot be accepted."
      }
    }
  } else if (status === 422) {
    testid = "invite-error-bad-id"
    headline = "Malformed invite id."
  } else if (status === 429) {
    testid = "invite-error-rate-limited"
    icon = <Clock size={14} />
    headline = "Too many attempts."
    body = "Please wait a minute before retrying. Repeated failures on the same invite are rate-limited to slow down brute-force probes."
  }

  return (
    <div
      className="rounded border border-[var(--destructive)]/40 bg-[var(--destructive)]/10 p-3 flex flex-col gap-2"
      data-testid={testid}
    >
      <div className="text-xs font-semibold text-[var(--destructive)] inline-flex items-center gap-1.5">
        {icon}
        <span data-testid="invite-error-headline">{headline}</span>
      </div>
      <p className="text-[11px] text-[var(--foreground)] leading-relaxed">
        {body}
      </p>
      {actions}
      <p
        className="text-[10px] text-[var(--muted-foreground)] opacity-80"
        data-testid="invite-error-status"
      >
        HTTP {err.status || "(network)"}
      </p>
    </div>
  )
}
