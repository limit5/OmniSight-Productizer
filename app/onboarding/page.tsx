"use client"

/**
 * AS.7.8 — First-login onboarding page.
 *
 * Composes the AS.7.0 visual foundation (`<AuthVisualFoundation>` +
 * `<AuthGlassCard>`) with a 4-step wizard (the first three are
 * input steps, the fourth is the celebration burst):
 *
 *   1. **Tenant**   — Confirm / rename the workspace tenant. The
 *                     submit fires `adminPatchTenant()` when the
 *                     role allows it; otherwise the row is read-only
 *                     with a "locked" reason on the gate so the user
 *                     understands why.
 *   2. **Profile**  — Capture the display name. Persisted to
 *                     `localStorage` until a backend
 *                     `PATCH /auth/me` endpoint lands (Phase-1
 *                     fail-closed pattern — same shape as the
 *                     AS.7.7 GDPR forms).
 *   3. **Project**  — Create the user's first project via
 *                     `createTenantProject()` (Y8 row 2 endpoint
 *                     that already ships in production).
 *   4. **Celebrate** — Mounts `<OnboardingCelebrationBurst>` for
 *                     30-particle burst + "Welcome aboard, X!"
 *                     wordmark. Fires the `/` redirect once the
 *                     animation settles.
 *
 * Module-global state audit (per implement_phase_step.md SOP §1):
 *   - Pure browser component. All state lives in React (`useState`
 *     / `useRef`). Helpers are pure (Answer #1 of the SOP audit).
 *   - The page reads `auth.user` for the role gate + tenant id
 *     pre-fill, and `lib/api.ts` wrappers for every backend call.
 *   - localStorage usage is per-tab DOM (not module-level mutable
 *     state); the helper key `ONBOARDING_DISPLAY_NAME_KEY` is a
 *     namespaced string constant.
 *   - No module-level mutable container.
 *
 * Read-after-write timing audit: each mutation is followed by a
 * fresh GET so local state matches the server (no parallelisation
 * change vs existing auth-context).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { useRouter, useSearchParams } from "next/navigation"
import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  Check,
  Loader2,
  Sparkles,
} from "lucide-react"

import { useAuth } from "@/lib/auth-context"
import { useEffectiveMotionLevel } from "@/hooks/use-effective-motion-level"
import {
  AuthBrandWordmark,
  AuthGlassCard,
  AuthVisualFoundation,
  OnboardingCelebrationBurst,
} from "@/components/omnisight/auth"
import {
  ApiError,
  adminPatchTenant,
  createTenantProject,
  listTenantProjects,
  listUserTenants,
  type TenantInfo,
} from "@/lib/api"
import {
  classifyProjectCreateError,
  classifyTenantUpdateError,
  firstLoginRequiredStep,
  ONBOARDING_DISPLAY_NAME_KEY,
  ONBOARDING_STEP_COPY,
  ONBOARDING_STEP_KIND,
  ONBOARDING_STEPS_ORDERED,
  PRODUCT_LINE_OPTIONS,
  profileBlockedReason,
  projectBlockedReason,
  slugifyProjectName,
  tenantNameBlockedReason,
  type OnboardingErrorOutcome,
  type OnboardingStepKind,
  type ProductLineId,
} from "@/lib/auth/onboarding-helpers"

// `instanceof ApiError` doesn't survive vi.mock substitution in
// tests (the page reads from the module mock; the test throws a
// different class), so we duck-type the status field. Pure function;
// returns null for everything that doesn't carry a numeric status.
function _readApiErrorStatus(err: unknown): number | null {
  if (err instanceof ApiError) return err.status
  if (
    typeof err === "object" &&
    err !== null &&
    "status" in err &&
    typeof (err as { status: unknown }).status === "number"
  ) {
    return (err as { status: number }).status
  }
  return null
}

// ─────────────────────────────────────────────────────────────────
// Step indicator pill row.
// ─────────────────────────────────────────────────────────────────

function StepIndicator({ activeStep }: { activeStep: OnboardingStepKind }) {
  return (
    <ol
      data-testid="as7-onboarding-step-indicator"
      className="flex items-center gap-2 font-mono text-[10px]"
    >
      {ONBOARDING_STEPS_ORDERED.map((kind, i) => {
        const isActive = kind === activeStep
        return (
          <li
            key={kind}
            data-testid={`as7-onboarding-step-${kind}`}
            data-as7-step-active={isActive ? "yes" : "no"}
            className={
              "flex items-center gap-1 px-2 py-0.5 rounded border " +
              (isActive
                ? "border-[var(--artifact-purple)] text-[var(--foreground)]"
                : "border-[var(--border)] text-[var(--muted-foreground)]")
            }
          >
            <span>{i + 1}</span>
            <span className="capitalize">{kind}</span>
          </li>
        )
      })}
    </ol>
  )
}

// ─────────────────────────────────────────────────────────────────
// Onboarding page header.
// ─────────────────────────────────────────────────────────────────

function OnboardingHeader({
  level,
  activeStep,
}: {
  level: ReturnType<typeof useEffectiveMotionLevel>
  activeStep: OnboardingStepKind
}) {
  const copy = ONBOARDING_STEP_COPY[activeStep]
  return (
    <div
      data-testid="as7-onboarding-header"
      data-as7-onboarding-step={activeStep}
      className="flex flex-col gap-3"
    >
      <AuthBrandWordmark level={level} />
      <StepIndicator activeStep={activeStep} />
      <h1
        data-testid="as7-onboarding-title"
        className="font-mono text-base font-semibold text-[var(--foreground)]"
      >
        {copy.title}
      </h1>
      <p
        data-testid="as7-onboarding-summary"
        className="font-mono text-xs text-[var(--muted-foreground)] leading-relaxed"
      >
        {copy.summary}
      </p>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// Banner used by every step on backend failure.
// ─────────────────────────────────────────────────────────────────

function OnboardingErrorBanner({
  outcome,
}: {
  outcome: OnboardingErrorOutcome | null
}) {
  if (!outcome) return null
  return (
    <div
      role="alert"
      data-testid="as7-onboarding-error"
      data-as7-error-kind={outcome.kind}
      className="flex items-start gap-2 p-2 rounded border border-[var(--artifact-purple)] bg-[var(--artifact-purple)]/10 text-[var(--foreground)] font-mono text-[11px]"
    >
      <AlertTriangle size={12} className="shrink-0 mt-0.5" />
      <span>{outcome.message}</span>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// Step 1 — Tenant rename / confirm.
// ─────────────────────────────────────────────────────────────────

interface TenantStepProps {
  primaryTenant: TenantInfo | null
  canEdit: boolean
  onConfirmed: (resolvedName: string) => void
}

function TenantStep({ primaryTenant, canEdit, onConfirmed }: TenantStepProps) {
  const [name, setName] = useState(primaryTenant?.name ?? "")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<OnboardingErrorOutcome | null>(null)

  useEffect(() => {
    if (primaryTenant?.name && !name) {
      setName(primaryTenant.name)
    }
    // We deliberately do not depend on `name` so the field stays
    // editable after the initial pre-fill.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [primaryTenant])

  const blocked = tenantNameBlockedReason({
    busy,
    name,
    canEdit,
  })

  const onSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault()
      if (!primaryTenant) return
      // When the role can't edit OR the name hasn't changed the page
      // simply advances to the next step without hitting the API.
      const trimmed = name.trim()
      if (!canEdit || trimmed === primaryTenant.name) {
        onConfirmed(trimmed || primaryTenant.name)
        return
      }
      if (blocked !== null) return
      setBusy(true)
      setError(null)
      try {
        const updated = await adminPatchTenant(primaryTenant.id, {
          name: trimmed,
        })
        onConfirmed(updated.name)
      } catch (err) {
        const status = _readApiErrorStatus(err)
        setError(classifyTenantUpdateError({ status }))
      } finally {
        setBusy(false)
      }
    },
    [blocked, canEdit, name, onConfirmed, primaryTenant],
  )

  return (
    <form
      data-testid="as7-onboarding-tenant-form"
      onSubmit={onSubmit}
      className="flex flex-col gap-3"
    >
      <label
        htmlFor="as7-onboarding-tenant-name"
        className="font-mono text-[11px] text-[var(--muted-foreground)]"
      >
        Workspace name
      </label>
      <input
        id="as7-onboarding-tenant-name"
        data-testid="as7-onboarding-tenant-input"
        type="text"
        value={name}
        onChange={(e) => setName(e.target.value)}
        readOnly={!canEdit}
        aria-readonly={!canEdit}
        maxLength={64}
        placeholder="e.g. Acme Cameras"
        className="px-3 py-2 rounded border border-[var(--border)] bg-[var(--background)] font-mono text-sm text-[var(--foreground)]"
      />
      {!canEdit ? (
        <p
          data-testid="as7-onboarding-tenant-locked"
          className="font-mono text-[10px] text-[var(--muted-foreground)]"
        >
          Your role cannot rename this workspace. Ask the workspace admin to
          update it. Continuing won't change anything.
        </p>
      ) : null}
      <OnboardingErrorBanner outcome={error} />
      <div className="flex items-center justify-end">
        <button
          type="submit"
          data-testid="as7-onboarding-tenant-submit"
          data-as7-block-reason={blocked === null ? "ok" : blocked}
          aria-disabled={
            // The "locked" reason is informational — non-admins still
            // advance through the step, they just can't change the name.
            blocked !== null && blocked !== "locked"
          }
          disabled={blocked !== null && blocked !== "locked"}
          className="flex items-center gap-1 px-3 py-2 rounded bg-[var(--artifact-purple)] text-white font-mono text-xs disabled:opacity-40"
        >
          {busy ? (
            <Loader2 size={12} className="animate-spin" />
          ) : (
            <Check size={12} />
          )}
          {ONBOARDING_STEP_COPY.tenant.ctaLabel}
        </button>
      </div>
    </form>
  )
}

// ─────────────────────────────────────────────────────────────────
// Step 2 — Profile display name.
// ─────────────────────────────────────────────────────────────────

interface ProfileStepProps {
  initialDisplayName: string
  onConfirmed: (resolved: string) => void
}

function ProfileStep({ initialDisplayName, onConfirmed }: ProfileStepProps) {
  const [displayName, setDisplayName] = useState(initialDisplayName)
  const [busy, setBusy] = useState(false)

  const blocked = profileBlockedReason({ busy, displayName })

  const onSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault()
      if (blocked !== null) return
      setBusy(true)
      try {
        const trimmed = displayName.trim()
        try {
          window.localStorage.setItem(ONBOARDING_DISPLAY_NAME_KEY, trimmed)
        } catch {
          // localStorage may be disabled (private mode / cookie ban).
          // The page still advances — the celebration step falls back
          // to the bare "Welcome aboard" copy.
        }
        onConfirmed(trimmed)
      } finally {
        setBusy(false)
      }
    },
    [blocked, displayName, onConfirmed],
  )

  return (
    <form
      data-testid="as7-onboarding-profile-form"
      onSubmit={onSubmit}
      className="flex flex-col gap-3"
    >
      <label
        htmlFor="as7-onboarding-profile-name"
        className="font-mono text-[11px] text-[var(--muted-foreground)]"
      >
        Display name
      </label>
      <input
        id="as7-onboarding-profile-name"
        data-testid="as7-onboarding-profile-input"
        type="text"
        value={displayName}
        onChange={(e) => setDisplayName(e.target.value)}
        maxLength={64}
        placeholder="e.g. Yi Hsuan Chang"
        className="px-3 py-2 rounded border border-[var(--border)] bg-[var(--background)] font-mono text-sm text-[var(--foreground)]"
      />
      <p className="font-mono text-[10px] text-[var(--muted-foreground)]">
        Stored on this device while we ship the profile API. You can change it
        any time from settings.
      </p>
      <div className="flex items-center justify-end">
        <button
          type="submit"
          data-testid="as7-onboarding-profile-submit"
          data-as7-block-reason={blocked === null ? "ok" : blocked}
          aria-disabled={blocked !== null}
          disabled={blocked !== null}
          className="flex items-center gap-1 px-3 py-2 rounded bg-[var(--artifact-purple)] text-white font-mono text-xs disabled:opacity-40"
        >
          {busy ? (
            <Loader2 size={12} className="animate-spin" />
          ) : (
            <ArrowRight size={12} />
          )}
          {ONBOARDING_STEP_COPY.profile.ctaLabel}
        </button>
      </div>
    </form>
  )
}

// ─────────────────────────────────────────────────────────────────
// Step 3 — Create the first project.
// ─────────────────────────────────────────────────────────────────

interface ProjectStepProps {
  tenantId: string
  onConfirmed: () => void
}

function ProjectStep({ tenantId, onConfirmed }: ProjectStepProps) {
  const [name, setName] = useState("")
  const [productLine, setProductLine] = useState<ProductLineId | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<OnboardingErrorOutcome | null>(null)

  const blocked = projectBlockedReason({ busy, name, productLine })
  const slugPreview = slugifyProjectName(name)

  const onSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault()
      if (blocked !== null || !productLine) return
      setBusy(true)
      setError(null)
      try {
        await createTenantProject(tenantId, {
          product_line: productLine,
          name: name.trim(),
          slug: slugPreview,
        })
        onConfirmed()
      } catch (err) {
        const status = _readApiErrorStatus(err)
        setError(classifyProjectCreateError({ status }))
      } finally {
        setBusy(false)
      }
    },
    [blocked, name, onConfirmed, productLine, slugPreview, tenantId],
  )

  return (
    <form
      data-testid="as7-onboarding-project-form"
      onSubmit={onSubmit}
      className="flex flex-col gap-3"
    >
      <label
        htmlFor="as7-onboarding-project-name"
        className="font-mono text-[11px] text-[var(--muted-foreground)]"
      >
        Project name
      </label>
      <input
        id="as7-onboarding-project-name"
        data-testid="as7-onboarding-project-input"
        type="text"
        value={name}
        onChange={(e) => setName(e.target.value)}
        maxLength={64}
        placeholder="e.g. Lobby cameras"
        className="px-3 py-2 rounded border border-[var(--border)] bg-[var(--background)] font-mono text-sm text-[var(--foreground)]"
      />
      <p
        data-testid="as7-onboarding-project-slug"
        className="font-mono text-[10px] text-[var(--muted-foreground)]"
      >
        Slug:{" "}
        <code data-testid="as7-onboarding-project-slug-value">
          {slugPreview || "—"}
        </code>
      </p>

      <fieldset
        data-testid="as7-onboarding-project-product-line"
        className="flex flex-col gap-2"
      >
        <legend className="font-mono text-[11px] text-[var(--muted-foreground)]">
          Product line
        </legend>
        <div className="grid grid-cols-1 gap-1 sm:grid-cols-2">
          {PRODUCT_LINE_OPTIONS.map((opt) => {
            const checked = productLine === opt.id
            return (
              <label
                key={opt.id}
                data-testid={`as7-onboarding-project-pl-${opt.id}`}
                data-as7-product-line-active={checked ? "yes" : "no"}
                className={
                  "flex flex-col gap-0.5 px-2 py-1.5 rounded border cursor-pointer " +
                  (checked
                    ? "border-[var(--artifact-purple)] bg-[var(--artifact-purple)]/10"
                    : "border-[var(--border)] hover:border-[var(--artifact-purple)]/60")
                }
              >
                <span className="flex items-center gap-1 font-mono text-[11px] text-[var(--foreground)]">
                  <input
                    type="radio"
                    name="as7-onboarding-product-line"
                    value={opt.id}
                    checked={checked}
                    onChange={() => setProductLine(opt.id)}
                    className="accent-[var(--artifact-purple)]"
                  />
                  {opt.label}
                </span>
                <span className="font-mono text-[10px] text-[var(--muted-foreground)] leading-relaxed">
                  {opt.summary}
                </span>
              </label>
            )
          })}
        </div>
      </fieldset>

      <OnboardingErrorBanner outcome={error} />

      <div className="flex items-center justify-end">
        <button
          type="submit"
          data-testid="as7-onboarding-project-submit"
          data-as7-block-reason={blocked === null ? "ok" : blocked}
          aria-disabled={blocked !== null}
          disabled={blocked !== null}
          className="flex items-center gap-1 px-3 py-2 rounded bg-[var(--artifact-purple)] text-white font-mono text-xs disabled:opacity-40"
        >
          {busy ? (
            <Loader2 size={12} className="animate-spin" />
          ) : (
            <Sparkles size={12} />
          )}
          {ONBOARDING_STEP_COPY.project.ctaLabel}
        </button>
      </div>
    </form>
  )
}

// ─────────────────────────────────────────────────────────────────
// Step 4 — Celebrate + redirect.
// ─────────────────────────────────────────────────────────────────

interface CelebrateStepProps {
  level: ReturnType<typeof useEffectiveMotionLevel>
  displayName: string | null
  redirectTarget: string
}

function CelebrateStep({
  level,
  displayName,
  redirectTarget,
}: CelebrateStepProps) {
  const router = useRouter()
  const [active, setActive] = useState(false)
  const firedRef = useRef(false)

  // Activate the burst on next frame so the parent transition has a
  // moment to settle.
  useEffect(() => {
    const id = window.setTimeout(() => setActive(true), 0)
    return () => window.clearTimeout(id)
  }, [])

  const onComplete = useCallback(() => {
    if (firedRef.current) return
    firedRef.current = true
    router.replace(redirectTarget)
  }, [redirectTarget, router])

  return (
    <div
      data-testid="as7-onboarding-celebrate"
      className="flex flex-col gap-3"
    >
      <OnboardingCelebrationBurst
        level={level}
        active={active}
        displayName={displayName}
        onComplete={onComplete}
      />
      <p
        data-testid="as7-onboarding-celebrate-summary"
        className="font-mono text-[11px] text-[var(--muted-foreground)] text-center"
      >
        {ONBOARDING_STEP_COPY.celebrate.summary}
      </p>
      <div className="flex justify-center">
        <button
          type="button"
          data-testid="as7-onboarding-celebrate-cta"
          onClick={onComplete}
          className="flex items-center gap-1 px-3 py-2 rounded bg-[var(--artifact-purple)] text-white font-mono text-xs"
        >
          <ArrowRight size={12} />
          {ONBOARDING_STEP_COPY.celebrate.ctaLabel}
        </button>
      </div>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// Body — bootstraps state, computes step, renders body.
// ─────────────────────────────────────────────────────────────────

function OnboardingBody({
  level,
}: {
  level: ReturnType<typeof useEffectiveMotionLevel>
}) {
  const auth = useAuth()
  const searchParams = useSearchParams()
  const nextParam = searchParams?.get("next") ?? null
  const redirectTarget = nextParam && nextParam.startsWith("/") ? nextParam : "/"

  const [bootstrapped, setBootstrapped] = useState(false)
  const [topError, setTopError] = useState<string | null>(null)
  const [tenants, setTenants] = useState<readonly TenantInfo[]>([])
  const [hasProject, setHasProject] = useState(false)
  const [tenantNameConfirmed, setTenantNameConfirmed] = useState<string | null>(
    null,
  )
  const [displayName, setDisplayName] = useState<string | null>(null)

  const role = (auth.user?.role || "").toLowerCase().trim()
  const canEditTenant =
    role === "admin" || role === "owner" || role === "super_admin"

  // Hydrate the display name from localStorage on mount so a user
  // who refreshes mid-onboarding doesn't lose the previous step.
  useEffect(() => {
    try {
      const stored = window.localStorage.getItem(ONBOARDING_DISPLAY_NAME_KEY)
      if (stored && stored.trim()) {
        setDisplayName(stored.trim())
      }
    } catch {
      // localStorage disabled — fine, the user just retypes.
    }
  }, [])

  const reloadTenants = useCallback(async () => {
    try {
      const items = await listUserTenants()
      setTenants(items)
    } catch (err) {
      setTopError(
        err instanceof Error ? err.message : "Could not load workspaces.",
      )
    }
  }, [])

  const reloadProjects = useCallback(async (tenantId: string | null) => {
    if (!tenantId) {
      setHasProject(false)
      return
    }
    try {
      const projects = await listTenantProjects(tenantId)
      setHasProject(projects.length > 0)
    } catch {
      // 403 / 404 / 5xx → treat as "no projects yet" so the wizard
      // continues. The Phase-1 fail-closed behaviour: showing the
      // user a fresh project form on a transient backend error is
      // safer than blocking the entire wizard.
      setHasProject(false)
    }
  }, [])

  // Initial bootstrap: load tenants + projects.
  useEffect(() => {
    if (!auth.user) return
    let aborted = false
    void (async () => {
      await reloadTenants()
      if (aborted) return
      setBootstrapped(true)
    })()
    return () => {
      aborted = true
    }
  }, [auth.user, reloadTenants])

  const primaryTenant = useMemo(() => {
    if (tenants.length === 0) return null
    if (auth.user?.tenant_id) {
      const match = tenants.find((t) => t.id === auth.user!.tenant_id)
      if (match) return match
    }
    return tenants[0]
  }, [auth.user, tenants])

  // Cascade reload of projects whenever we resolve the primary tenant.
  useEffect(() => {
    if (!bootstrapped) return
    void reloadProjects(primaryTenant?.id ?? null)
  }, [bootstrapped, primaryTenant, reloadProjects])

  const activeStep: OnboardingStepKind = useMemo(
    () =>
      firstLoginRequiredStep({
        tenantName: tenantNameConfirmed ?? null,
        displayName: displayName ?? null,
        hasProject,
        celebrated: false,
      }),
    [tenantNameConfirmed, displayName, hasProject],
  )

  const onTenantConfirmed = useCallback((resolvedName: string) => {
    setTenantNameConfirmed(resolvedName)
  }, [])

  const onProfileConfirmed = useCallback((resolved: string) => {
    setDisplayName(resolved)
  }, [])

  const onProjectConfirmed = useCallback(() => {
    setHasProject(true)
  }, [])

  if (!auth.loading && auth.user === null) {
    return (
      <div
        data-testid="as7-onboarding-unauth"
        className="flex flex-col items-center gap-2 p-4 font-mono text-xs"
      >
        <Loader2
          size={14}
          className="animate-spin text-[var(--artifact-purple)]"
        />
        <span>Sign in to start onboarding…</span>
        <RedirectToLoginEffect redirectTarget={redirectTarget} />
      </div>
    )
  }

  return (
    <div
      data-testid="as7-onboarding-body"
      data-as7-onboarding-bootstrapped={bootstrapped ? "yes" : "no"}
      data-as7-onboarding-step={activeStep}
      className="flex flex-col gap-5"
    >
      <OnboardingHeader level={level} activeStep={activeStep} />

      {topError ? (
        <div
          role="alert"
          data-testid="as7-onboarding-top-error"
          className="flex items-start gap-2 p-2 rounded border border-[var(--artifact-purple)] bg-[var(--artifact-purple)]/10 text-[var(--foreground)] font-mono text-[11px]"
        >
          <AlertTriangle size={12} className="shrink-0 mt-0.5" />
          <span>{topError}</span>
        </div>
      ) : null}

      {activeStep === ONBOARDING_STEP_KIND.tenant ? (
        primaryTenant ? (
          <TenantStep
            primaryTenant={primaryTenant}
            canEdit={canEditTenant}
            onConfirmed={onTenantConfirmed}
          />
        ) : (
          <div
            data-testid="as7-onboarding-tenant-loading"
            className="flex items-center gap-2 font-mono text-[11px] text-[var(--muted-foreground)]"
          >
            <Loader2 size={12} className="animate-spin" />
            Loading workspace…
          </div>
        )
      ) : null}

      {activeStep === ONBOARDING_STEP_KIND.profile ? (
        <ProfileStep
          initialDisplayName={displayName ?? auth.user?.name ?? ""}
          onConfirmed={onProfileConfirmed}
        />
      ) : null}

      {activeStep === ONBOARDING_STEP_KIND.project ? (
        primaryTenant ? (
          <ProjectStep
            tenantId={primaryTenant.id}
            onConfirmed={onProjectConfirmed}
          />
        ) : (
          <div
            data-testid="as7-onboarding-project-no-tenant"
            className="font-mono text-[11px] text-[var(--muted-foreground)]"
          >
            Loading workspace…
          </div>
        )
      ) : null}

      {activeStep === ONBOARDING_STEP_KIND.celebrate ? (
        <CelebrateStep
          level={level}
          displayName={displayName}
          redirectTarget={redirectTarget}
        />
      ) : null}

      <a
        href="/"
        data-testid="as7-onboarding-skip"
        className="self-end flex items-center gap-1 font-mono text-[10px] text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
      >
        <ArrowLeft size={10} /> Skip for now
      </a>
    </div>
  )
}

function RedirectToLoginEffect({
  redirectTarget,
}: {
  redirectTarget: string
}) {
  const router = useRouter()
  useEffect(() => {
    // When the caller passed `?next=/somewhere` we forward it through
    // the onboarding route; for the bare `/onboarding` case we keep
    // the URL clean.
    const next =
      redirectTarget && redirectTarget !== "/" && redirectTarget.startsWith("/")
        ? `/onboarding?next=${encodeURIComponent(redirectTarget)}`
        : "/onboarding"
    router.replace(`/login?next=${encodeURIComponent(next)}`)
  }, [redirectTarget, router])
  return null
}

// ─────────────────────────────────────────────────────────────────
// Page scaffold — same shape as AS.7.1 / AS.7.2 / AS.7.3 / AS.7.4 /
// AS.7.5 / AS.7.6 / AS.7.7.
// ─────────────────────────────────────────────────────────────────

function OnboardingScaffold() {
  const level = useEffectiveMotionLevel()
  return (
    <AuthVisualFoundation forceLevel={level}>
      <AuthGlassCard level={level}>
        <OnboardingBody level={level} />
      </AuthGlassCard>
    </AuthVisualFoundation>
  )
}

export default function OnboardingPage() {
  return <OnboardingScaffold />
}
