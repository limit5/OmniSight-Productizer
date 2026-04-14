"use client"

/**
 * /login — minimal email + password form.
 *
 * Internet-exposure auth gate. The dashboard's HomePage redirects
 * here when whoami returns 401 in session/strict mode. On success
 * the AuthProvider's user state flips, and a useEffect on this
 * page sends the operator back to "/" (or the `next` query param if
 * a deep-link triggered the redirect).
 */

import { useEffect, useState } from "react"
import { useRouter, useSearchParams } from "next/navigation"
import { Lock, Mail, AlertCircle, Loader2 } from "lucide-react"
import { AuthProvider, useAuth } from "@/lib/auth-context"

function LoginForm() {
  const router = useRouter()
  const search = useSearchParams()
  const next = search.get("next") || "/"
  const auth = useAuth()
  const [email, setEmail] = useState("")
  const [password, setPassword] = useState("")
  const [busy, setBusy] = useState(false)

  // Already logged in? Bounce to next so a stale tab doesn't sit on
  // the login form after the cookie became valid in another window.
  useEffect(() => {
    if (!auth.loading && auth.user) router.replace(next)
  }, [auth.loading, auth.user, next, router])

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (busy) return
    setBusy(true)
    try {
      const ok = await auth.login(email, password)
      if (ok) router.replace(next)
    } finally {
      setBusy(false)
    }
  }

  return (
    <main className="min-h-screen flex items-center justify-center p-6 bg-[var(--background)]">
      <form
        onSubmit={onSubmit}
        className="w-full max-w-sm rounded-lg border border-[var(--border)] bg-[var(--card)] p-6 flex flex-col gap-4"
      >
        <div className="text-center">
          <h1 className="font-mono text-lg font-semibold text-[var(--foreground)]">
            OmniSight
          </h1>
          <p className="font-mono text-xs text-[var(--muted-foreground)] mt-1">
            Sign in to continue
          </p>
        </div>

        <label className="flex flex-col gap-1">
          <span className="font-mono text-[10px] tracking-wider text-[var(--muted-foreground)]">
            EMAIL
          </span>
          <div className="flex items-center gap-2 px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--background)] focus-within:ring-1 focus-within:ring-[var(--artifact-purple)]">
            <Mail size={14} className="text-[var(--muted-foreground)]" />
            <input
              type="email"
              autoComplete="email"
              autoFocus
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="flex-1 bg-transparent outline-none font-mono text-sm text-[var(--foreground)]"
              placeholder="you@example.com"
            />
          </div>
        </label>

        <label className="flex flex-col gap-1">
          <span className="font-mono text-[10px] tracking-wider text-[var(--muted-foreground)]">
            PASSWORD
          </span>
          <div className="flex items-center gap-2 px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--background)] focus-within:ring-1 focus-within:ring-[var(--artifact-purple)]">
            <Lock size={14} className="text-[var(--muted-foreground)]" />
            <input
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="flex-1 bg-transparent outline-none font-mono text-sm text-[var(--foreground)]"
            />
          </div>
        </label>

        {auth.error && (
          <div
            role="alert"
            className="flex items-start gap-2 p-2 rounded border border-[var(--destructive)] bg-[var(--destructive)]/10 text-[var(--destructive)] font-mono text-xs"
          >
            <AlertCircle size={14} className="shrink-0 mt-0.5" />
            <span>{auth.error}</span>
          </div>
        )}

        <button
          type="submit"
          disabled={busy || !email || !password}
          className="flex items-center justify-center gap-2 px-3 py-2 rounded bg-[var(--artifact-purple)] text-white font-mono text-sm font-semibold hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {busy ? <Loader2 size={14} className="animate-spin" /> : null}
          Sign in
        </button>

        <p className="font-mono text-[10px] text-[var(--muted-foreground)] text-center leading-relaxed">
          First boot? Bootstrap admin email is whatever you set in
          <code className="mx-1">OMNISIGHT_ADMIN_EMAIL</code>
          (default: <code>admin@omnisight.local</code>).
        </p>
      </form>
    </main>
  )
}

export default function LoginPage() {
  // The login page lives outside the dashboard's normal layout, so
  // it provides its own AuthProvider — that way it can run before
  // the operator has a session and still drive whoami through the
  // same context other pages use.
  return (
    <AuthProvider>
      <LoginForm />
    </AuthProvider>
  )
}
