"use client"

/**
 * L1 — First-install bootstrap wizard (shell).
 *
 * Mirrors the four gates exposed by `backend.bootstrap.get_bootstrap_status`
 * plus a final Finalize transition driven by
 * `POST /api/v1/bootstrap/finalize`. Each step body is a placeholder —
 * L2 (admin password), L3 (LLM provider), L4 (CF tunnel) and L5 (smoke)
 * fill them in; this page is the navigation + status shell they plug into.
 *
 * The wizard polls `GET /api/v1/bootstrap/status` on mount + after every
 * step so the green/red markers reflect live backend signals.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { useRouter } from "next/navigation"
import {
  Activity,
  AlertCircle,
  Check,
  ChevronLeft,
  ChevronRight,
  CircleDashed,
  Database,
  Globe,
  Layers,
  Loader2,
  RefreshCw,
  Rocket,
  Shield,
  KeyRound,
  Cloud,
  FlaskConical,
  Radio,
  Sparkles,
  Bot,
  Server,
  Cpu,
  GitBranch,
} from "lucide-react"
import {
  BOOTSTRAP_ADMIN_PASSWORD_KIND_COPY,
  BOOTSTRAP_INIT_TENANT_KIND_COPY,
  BOOTSTRAP_PROVIDER_KEY_URL,
  BOOTSTRAP_PROVISION_KIND_COPY,
  BOOTSTRAP_START_SERVICES_KIND_COPY,
  BOOTSTRAP_VERTICAL_PRIMARY_ENTRY,
  BootstrapAdminPasswordError,
  BootstrapInitTenantError,
  BootstrapLlmProvisionError,
  BootstrapStartServicesError,
  bootstrapCfTunnelSkip,
  bootstrapDetectOllama,
  bootstrapInitTenant,
  bootstrapLlmProvision,
  bootstrapParallelHealthCheck,
  bootstrapRecordVerticalSetup,
  bootstrapSetAdminPassword,
  bootstrapSmokeSubset,
  bootstrapStartServices,
  createInstallJob,
  finalizeBootstrap,
  getBootstrapStatus,
  testGitForgeToken,
  updateSettings,
  type InstallJob,
  type GitForgeTokenTestResult,
  type BootstrapAdminPasswordKind,
  type BootstrapGates,
  type BootstrapHealthCheckResult,
  type BootstrapFrontendFreshness,
  type BootstrapInitTenantKind,
  type BootstrapInitTenantRequest,
  type BootstrapInitTenantResponse,
  type BootstrapLlmProvisionKind,
  type BootstrapLlmProvisionRequest,
  type BootstrapLlmProvisionResponse,
  type BootstrapOllamaDetectResponse,
  type BootstrapParallelHealthCheckResponse,
  type BootstrapSmokeSubsetResponse,
  type BootstrapStartServicesKind,
  type BootstrapStartServicesResponse,
  type BootstrapStatusResponse,
} from "@/lib/api"
import {
  estimatePasswordStrength,
  PASSWORD_MIN_LENGTH,
  PASSWORD_MIN_SCORE,
} from "@/lib/password_strength"
import CloudflareTunnelSetup from "@/components/omnisight/cloudflare-tunnel-setup"
import BootstrapVerticalStep, {
  type BootstrapVerticalCommitPayload,
  type BootstrapVerticalId,
} from "@/components/omnisight/bootstrap-vertical-step"
import AndroidApiSelector, {
  DEFAULT_ANDROID_API_SELECTION,
  type AndroidApiSelection,
} from "@/components/omnisight/android-api-selector"
import { useCinemaMode } from "@/lib/use-cinema-mode"

// ─── Step definitions ────────────────────────────────────────────────

type StepId =
  | "admin_password"
  | "init_tenant"
  | "llm_provider"
  | "cf_tunnel"
  | "git_forge"
  | "vertical_setup"
  | "services_ready"
  | "frontend_freshness"
  | "smoke"
  | "finalize"

interface StepDef {
  id: StepId
  title: string
  subtitle: string
  icon: React.ComponentType<{ size?: number; className?: string }>
  /**
   * Returns true if this step is satisfied by current backend signals.
   *
   * For ``services_ready`` the backend's ``BootstrapGates`` doesn't carry
   * a dedicated boolean (the parallel-health-check is a probe, not a
   * gate per backend.routers.bootstrap), so the step's tick is driven
   * by the latest local probe result instead — passed through
   * ``localGreen``.
   */
  isGreen: (
    g: BootstrapGates,
    finalized: boolean,
    localGreen: Record<StepId, boolean>,
    status?: BootstrapStatusResponse,
  ) => boolean
}

const STEPS: StepDef[] = [
  {
    id: "admin_password",
    title: "Admin Password",
    subtitle: "Rotate the shipping default credential",
    icon: KeyRound,
    isGreen: (g) => !g.admin_password_default,
  },
  {
    id: "init_tenant",
    title: "Initialize your organization",
    subtitle: "Create your real tenant + super-admin (or skip for t-default)",
    icon: Database,
    // Y7 #283 — optional step (not a finalize gate). Driven by
    // localGreen so the pill flips green when the operator either
    // creates the tenant or explicitly opts to keep t-default.
    isGreen: (_g, _finalized, localGreen) => localGreen.init_tenant === true,
  },
  {
    id: "llm_provider",
    title: "LLM Provider",
    subtitle: "Pick a provider and supply an API key",
    icon: Shield,
    isGreen: (g) => g.llm_provider_configured,
  },
  {
    id: "cf_tunnel",
    title: "Cloudflare Tunnel",
    subtitle: "Provision remote access (or skip for LAN-only)",
    icon: Cloud,
    isGreen: (g) => g.cf_tunnel_configured,
  },
  {
    id: "git_forge",
    title: "Git Forge (optional)",
    subtitle: "Connect GitHub / GitLab / Gerrit now, or skip for later",
    icon: GitBranch,
    // Optional step — not a finalize gate. Driven by localGreen so the
    // pill flips green when the operator either configures a forge or
    // explicitly opts out via the "Skip" button.
    isGreen: (_g, _finalized, localGreen) => localGreen.git_forge === true,
  },
  {
    id: "vertical_setup",
    title: "Verticals (optional)",
    subtitle:
      "Pick platforms (Mobile / Embedded / Web / Software / Cross-toolchain) — or skip",
    icon: Layers,
    // BS.9.2 — optional intermediate step matching backend
    // ``STEP_VERTICAL_SETUP`` (see ``backend/bootstrap.py``). NOT in
    // ``REQUIRED_STEPS``: finalize never blocks on it, so existing
    // installs that finalized pre-BS.9 stay green and the wizard
    // auto-redirects them away from ``/bootstrap`` before this pill
    // is ever rendered. New installs land here as an opt-in picker
    // and either commit a payload (BS.9.3 ships the multi-pick) or
    // dismiss via the Skip button — both flip ``localGreen``.
    isGreen: (_g, _finalized, localGreen) =>
      localGreen.vertical_setup === true,
  },
  {
    id: "services_ready",
    title: "Service Health",
    subtitle: "Verify backend / frontend / DB / tunnel are all live",
    icon: Activity,
    isGreen: (_g, _finalized, localGreen) => localGreen.services_ready === true,
  },
  {
    id: "frontend_freshness",
    title: "Frontend Freshness",
    subtitle: "Compare the production bundle commit with master HEAD",
    icon: GitBranch,
    isGreen: (_g, _finalized, _localGreen, status) =>
      status?.frontend_freshness.status === "fresh",
  },
  {
    id: "smoke",
    title: "Smoke Test",
    subtitle: "Run the end-to-end install check",
    icon: FlaskConical,
    isGreen: (g) => g.smoke_passed,
  },
  {
    id: "finalize",
    title: "Finalize",
    subtitle: "Close the wizard and unlock the app",
    icon: Rocket,
    isGreen: (_g, finalized) => finalized,
  },
]

// ─── Presentational helpers ──────────────────────────────────────────

function StepPill({
  def,
  state,
  active,
  onClick,
  index,
}: {
  def: StepDef
  state: "green" | "pending" | "active"
  active: boolean
  onClick: () => void
  index: number
}) {
  const Icon = def.icon
  const border =
    state === "green"
      ? "border-[var(--status-green)]"
      : active
        ? "border-[var(--artifact-purple)]"
        : "border-[var(--border)]"
  const glyph =
    state === "green" ? (
      <Check size={14} className="text-[var(--status-green)]" />
    ) : active ? (
      <CircleDashed size={14} className="text-[var(--artifact-purple)] animate-pulse" />
    ) : (
      <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
        {index + 1}
      </span>
    )
  return (
    <button
      type="button"
      onClick={onClick}
      aria-current={active ? "step" : undefined}
      data-state={state}
      data-testid={`bootstrap-step-${def.id}`}
      className={`flex items-center gap-2 w-full text-left p-2 rounded border ${border} bg-[var(--background)] hover:bg-[var(--muted)]/30 transition`}
    >
      <span className="flex items-center justify-center w-6 h-6 rounded-full border border-[var(--border)]">
        {glyph}
      </span>
      <Icon size={14} className="text-[var(--muted-foreground)]" />
      <span className="flex-1 min-w-0">
        <span className="block font-mono text-xs text-[var(--foreground)] truncate">
          {def.title}
        </span>
        <span className="block font-mono text-[10px] text-[var(--muted-foreground)] truncate">
          {def.subtitle}
        </span>
      </span>
    </button>
  )
}

function AdminPasswordErrorBanner({
  kind,
  detail,
}: {
  kind: BootstrapAdminPasswordKind
  detail: string
}) {
  const copy = BOOTSTRAP_ADMIN_PASSWORD_KIND_COPY[kind]
  return (
    <div
      role="alert"
      data-testid="bootstrap-admin-password-error"
      data-kind={kind}
      className="flex flex-col gap-1 p-3 rounded border border-[var(--destructive)] bg-[var(--destructive)]/10"
    >
      <div className="flex items-center gap-2 font-mono text-[11px] font-semibold text-[var(--destructive)]">
        <AlertCircle size={12} /> {copy.title}
      </div>
      <p className="font-mono text-[11px] text-[var(--destructive)] break-words">
        {detail}
      </p>
      <p className="font-mono text-[10px] text-[var(--muted-foreground)] leading-relaxed">
        {copy.hint}
      </p>
      {kind === "password_too_weak" && (
        <p
          data-testid="bootstrap-admin-password-weak-tips"
          className="font-mono text-[10px] text-[var(--muted-foreground)] leading-relaxed"
        >
          Tip: combine an unusual phrase with numbers + symbols. Avoid reusing
          a password you&rsquo;ve used elsewhere — K7&rsquo;s zxcvbn checker
          heavily penalises known breach-corpus entries.
        </p>
      )}
    </div>
  )
}

function AdminPasswordStep({
  alreadyGreen,
  onRotated,
}: {
  alreadyGreen: boolean
  onRotated: () => Promise<unknown>
}) {
  const [currentPassword, setCurrentPassword] = useState("omnisight-admin")
  const [newPassword, setNewPassword] = useState("")
  const [confirmPassword, setConfirmPassword] = useState("")
  const [busy, setBusy] = useState(false)
  // ``errKind`` is set when the backend returned a classified failure so
  // the UI can render a kind-keyed banner (see
  // ``AdminPasswordErrorBanner``). ``localError`` covers the fall-through
  // case — unclassified network / transport errors that do not map to a
  // kind. Both are cleared before every submit.
  const [errKind, setErrKind] = useState<BootstrapAdminPasswordKind | null>(null)
  const [errDetail, setErrDetail] = useState<string>("")
  const [localError, setLocalError] = useState<string | null>(null)
  const [success, setSuccess] = useState(false)

  const strength = useMemo(
    () => estimatePasswordStrength(newPassword),
    [newPassword],
  )
  const mismatch =
    confirmPassword.length > 0 && newPassword !== confirmPassword
  const tooShort =
    newPassword.length > 0 && newPassword.length < PASSWORD_MIN_LENGTH
  const tooWeak =
    newPassword.length >= PASSWORD_MIN_LENGTH && !strength.passes
  const canSubmit =
    !busy &&
    currentPassword.length > 0 &&
    strength.passes &&
    confirmPassword === newPassword

  const handleSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault()
      if (!canSubmit) return
      setBusy(true)
      setLocalError(null)
      setErrKind(null)
      setErrDetail("")
      setSuccess(false)
      try {
        await bootstrapSetAdminPassword(currentPassword, newPassword)
        setSuccess(true)
        setCurrentPassword("")
        setNewPassword("")
        setConfirmPassword("")
        await onRotated()
      } catch (err) {
        if (err instanceof BootstrapAdminPasswordError) {
          setErrKind(err.kind)
          setErrDetail(err.detail || err.message)
        } else {
          const msg = err instanceof Error ? err.message : String(err)
          setLocalError(msg)
        }
      } finally {
        setBusy(false)
      }
    },
    [canSubmit, currentPassword, newPassword, onRotated],
  )

  if (alreadyGreen) {
    return (
      <div
        data-testid="bootstrap-admin-password-complete"
        className="flex flex-col gap-2 p-4 rounded border border-[var(--status-green)] bg-[var(--background)]"
      >
        <div className="flex items-center gap-2 font-mono text-xs text-[var(--status-green)]">
          <Check size={14} /> Admin password rotated
        </div>
        <p className="font-mono text-[11px] text-[var(--muted-foreground)] leading-relaxed">
          The shipping default credential has been replaced. Continue to
          the next step.
        </p>
      </div>
    )
  }

  return (
    <form
      onSubmit={handleSubmit}
      data-testid="bootstrap-admin-password-form"
      className="flex flex-col gap-3 p-4 rounded border border-[var(--border)] bg-[var(--background)]"
    >
      <div className="flex items-center gap-2 font-mono text-[10px] tracking-wider text-[var(--muted-foreground)]">
        <span>GATE</span>
        <code className="px-1.5 py-0.5 rounded bg-[var(--muted)]/50 text-[var(--foreground)]">
          admin_password_default === false
        </code>
      </div>
      <p className="font-mono text-[11px] text-[var(--muted-foreground)] leading-relaxed">
        The shipping install creates <code>admin@omnisight.local</code> with
        the well-known password <code>omnisight-admin</code>. Rotate it now —
        all other APIs stay 428-locked until this gate clears.
      </p>

      <label className="flex flex-col gap-1">
        <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
          Current password
        </span>
        <input
          type="password"
          data-testid="bootstrap-admin-password-current"
          value={currentPassword}
          onChange={(e) => setCurrentPassword(e.target.value)}
          autoComplete="current-password"
          required
          className="font-mono text-xs px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--card)] text-[var(--foreground)]"
        />
      </label>

      <label className="flex flex-col gap-1">
        <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
          New password (min 12 chars, zxcvbn ≥ 3)
        </span>
        <input
          type="password"
          data-testid="bootstrap-admin-password-new"
          value={newPassword}
          onChange={(e) => setNewPassword(e.target.value)}
          autoComplete="new-password"
          minLength={12}
          required
          className="font-mono text-xs px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--card)] text-[var(--foreground)]"
        />
      </label>

      <label className="flex flex-col gap-1">
        <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
          Confirm new password
        </span>
        <input
          type="password"
          data-testid="bootstrap-admin-password-confirm"
          value={confirmPassword}
          onChange={(e) => setConfirmPassword(e.target.value)}
          autoComplete="new-password"
          minLength={12}
          required
          className="font-mono text-xs px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--card)] text-[var(--foreground)]"
        />
      </label>

      {newPassword.length > 0 && (
        <div
          data-testid="bootstrap-admin-password-strength"
          data-score={strength.score}
          data-passes={strength.passes ? "true" : "false"}
          className="flex flex-col gap-1"
        >
          <div className="flex items-center gap-2">
            <div className="flex gap-1 flex-1" aria-hidden>
              {[0, 1, 2, 3, 4].map((i) => (
                <span
                  key={i}
                  className={`h-1.5 flex-1 rounded ${
                    i < strength.score
                      ? strength.passes
                        ? "bg-[var(--status-green)]"
                        : "bg-[var(--status-yellow,#d97706)]"
                      : "bg-[var(--muted)]"
                  }`}
                />
              ))}
            </div>
            <span
              className={`font-mono text-[10px] uppercase tracking-wider ${
                strength.passes
                  ? "text-[var(--status-green)]"
                  : "text-[var(--muted-foreground)]"
              }`}
            >
              {strength.label} · {strength.score}/4
            </span>
          </div>
          <p className="font-mono text-[10px] text-[var(--muted-foreground)] leading-relaxed">
            {strength.hint} (server re-checks with K7 zxcvbn ≥{" "}
            {PASSWORD_MIN_SCORE}.)
          </p>
        </div>
      )}
      {tooShort && (
        <p className="font-mono text-[11px] text-[var(--destructive)]">
          New password must be at least {PASSWORD_MIN_LENGTH} characters.
        </p>
      )}
      {tooWeak && !tooShort && (
        <p
          data-testid="bootstrap-admin-password-weak"
          className="font-mono text-[11px] text-[var(--destructive)]"
        >
          New password is too guessable — score ≥ {PASSWORD_MIN_SCORE} required.
        </p>
      )}
      {mismatch && (
        <p className="font-mono text-[11px] text-[var(--destructive)]">
          New password and confirmation do not match.
        </p>
      )}
      {errKind && (
        <AdminPasswordErrorBanner kind={errKind} detail={errDetail} />
      )}
      {!errKind && localError && (
        <p
          role="alert"
          data-testid="bootstrap-admin-password-error"
          data-kind="unclassified"
          className="font-mono text-[11px] text-[var(--destructive)] break-words"
        >
          {localError}
        </p>
      )}
      {success && (
        <p className="font-mono text-[11px] text-[var(--status-green)]">
          Password rotated — refreshing gate status…
        </p>
      )}

      <button
        type="submit"
        data-testid="bootstrap-admin-password-submit"
        disabled={!canSubmit}
        className="self-start flex items-center gap-2 px-3 py-2 rounded bg-[var(--artifact-purple)] text-white font-mono text-xs font-semibold hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed"
      >
        {busy ? <Loader2 size={12} className="animate-spin" /> : <KeyRound size={12} />}
        Rotate admin password
      </button>
    </form>
  )
}


// ─── L3 — LLM Provider menu (provider picker only) ───────────────────
//
// Scope of this slot is the picker UI itself — the API key form, key
// validation (`provider.ping()`), Ollama localhost detection, secrets
// storage, and error handling are tracked as separate L3 sub-tasks and
// land in follow-up slots.

type LlmProviderId = "anthropic" | "openai" | "ollama" | "azure"

interface LlmProviderOption {
  id: LlmProviderId
  label: string
  tagline: string
  hint: string
  icon: React.ComponentType<{ size?: number; className?: string }>
}

const LLM_PROVIDERS: LlmProviderOption[] = [
  {
    id: "anthropic",
    label: "Anthropic",
    tagline: "Claude (Opus / Sonnet / Haiku)",
    hint: "Hosted API · requires an Anthropic API key",
    icon: Sparkles,
  },
  {
    id: "openai",
    label: "OpenAI",
    tagline: "GPT-4 / GPT-4o family",
    hint: "Hosted API · requires an OpenAI API key",
    icon: Bot,
  },
  {
    id: "ollama",
    label: "Ollama (local)",
    tagline: "Local models on this host",
    hint: "Detects localhost:11434 · no key required",
    icon: Server,
  },
  {
    id: "azure",
    label: "Azure OpenAI",
    tagline: "Azure-hosted OpenAI deployment",
    hint: "Requires endpoint + deployment + API key",
    icon: Cpu,
  },
]

const OLLAMA_DEFAULT_BASE_URL = "http://localhost:11434"

const OLLAMA_KIND_HINTS: Record<string, string> = {
  network_unreachable:
    "Ollama did not respond at that URL — is the daemon running?",
  bad_request: "Ollama replied with an unexpected status.",
  provider_error: "Ollama returned a server error.",
  key_invalid: "Ollama rejected the probe — unusual for a local daemon.",
  quota_exceeded: "Ollama reported a rate-limit response.",
}

function OllamaDetectPanel({
  baseUrl,
  onBaseUrlChange,
  onModelSelected,
  selectedModel,
}: {
  baseUrl: string
  onBaseUrlChange: (v: string) => void
  onModelSelected: (model: string) => void
  selectedModel: string
}) {
  const [probe, setProbe] = useState<BootstrapOllamaDetectResponse | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const runProbe = useCallback(
    async (targetUrl: string) => {
      setBusy(true)
      setErr(null)
      try {
        const result = await bootstrapDetectOllama(targetUrl || undefined)
        setProbe(result)
        // Auto-pick the first model if nothing is selected yet — lets the
        // operator submit the wizard step without an extra click when the
        // host already has a single pulled model.
        if (result.reachable && result.models.length > 0 && !selectedModel) {
          onModelSelected(result.models[0])
        }
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e)
        setErr(msg)
        setProbe(null)
      } finally {
        setBusy(false)
      }
    },
    [onModelSelected, selectedModel],
  )

  // Auto-probe on mount using the default URL so the operator sees an
  // immediate reachable/unreachable indicator without a manual click.
  useEffect(() => {
    void runProbe(baseUrl || OLLAMA_DEFAULT_BASE_URL)
    // We intentionally run this once per mount — further probes go via
    // the explicit "Re-detect" button so typing in the URL field does
    // not spam the local daemon.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const state: "idle" | "probing" | "reachable" | "unreachable" = busy
    ? "probing"
    : probe == null
      ? "idle"
      : probe.reachable
        ? "reachable"
        : "unreachable"

  return (
    <div
      data-testid="bootstrap-ollama-detect"
      data-state={state}
      className="flex flex-col gap-2 p-3 rounded border border-[var(--border)] bg-[var(--background)]"
    >
      <div className="flex items-center gap-2 font-mono text-[10px] tracking-wider text-[var(--muted-foreground)]">
        <span>OLLAMA</span>
        <code className="px-1.5 py-0.5 rounded bg-[var(--muted)]/50 text-[var(--foreground)]">
          GET {(baseUrl || OLLAMA_DEFAULT_BASE_URL).replace(/\/$/, "")}/api/tags
        </code>
      </div>

      <label className="flex flex-col gap-1">
        <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
          Base URL (leave blank for localhost:11434)
        </span>
        <input
          type="text"
          data-testid="bootstrap-ollama-base-url"
          value={baseUrl}
          onChange={(e) => onBaseUrlChange(e.target.value)}
          placeholder={OLLAMA_DEFAULT_BASE_URL}
          className="font-mono text-xs px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--card)] text-[var(--foreground)]"
        />
      </label>

      <div className="flex items-center gap-2">
        <button
          type="button"
          data-testid="bootstrap-ollama-detect-button"
          onClick={() => void runProbe(baseUrl || OLLAMA_DEFAULT_BASE_URL)}
          disabled={busy}
          className="flex items-center gap-1 font-mono text-[11px] px-2 py-1 rounded border border-[var(--border)] hover:bg-[var(--muted)]/40 disabled:opacity-40"
        >
          {busy ? <Loader2 size={12} className="animate-spin" /> : <Server size={12} />}
          {probe == null ? "Detect" : "Re-detect"}
        </button>
        {state === "reachable" && (
          <span
            data-testid="bootstrap-ollama-reachable"
            className="flex items-center gap-1 font-mono text-[11px] text-[var(--status-green)]"
          >
            <Check size={12} /> reachable · {probe?.latency_ms}ms
          </span>
        )}
        {state === "unreachable" && (
          <span
            data-testid="bootstrap-ollama-unreachable"
            className="flex items-center gap-1 font-mono text-[11px] text-[var(--destructive)]"
          >
            <AlertCircle size={12} />
            {OLLAMA_KIND_HINTS[probe?.kind ?? ""] ?? "not reachable"}
          </span>
        )}
      </div>

      {err && (
        <p
          role="alert"
          data-testid="bootstrap-ollama-error"
          className="font-mono text-[11px] text-[var(--destructive)] break-words"
        >
          {err}
        </p>
      )}

      {probe && probe.reachable && (
        <div className="flex flex-col gap-1">
          <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
            Available models ({probe.models.length})
          </span>
          {probe.models.length === 0 ? (
            <p
              data-testid="bootstrap-ollama-no-models"
              className="font-mono text-[11px] text-[var(--muted-foreground)] italic"
            >
              Host is reachable but no models are pulled. Run{" "}
              <code>ollama pull &lt;model&gt;</code> and click Re-detect.
            </p>
          ) : (
            <select
              data-testid="bootstrap-ollama-model-select"
              value={selectedModel}
              onChange={(e) => onModelSelected(e.target.value)}
              className="font-mono text-xs px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--card)] text-[var(--foreground)]"
            >
              <option value="">— select a model —</option>
              {probe.models.map((m) => (
                <option key={m} value={m} data-testid={`bootstrap-ollama-model-option-${m}`}>
                  {m}
                </option>
              ))}
            </select>
          )}
        </div>
      )}

      {probe && !probe.reachable && (
        <p className="font-mono text-[10px] text-[var(--muted-foreground)] leading-relaxed">
          Install Ollama from{" "}
          <code>https://ollama.com/download</code>, start{" "}
          <code>ollama serve</code>, then click Re-detect.
        </p>
      )}
    </div>
  )
}

function ProvisionErrorBanner({
  kind,
  detail,
  providerId,
}: {
  kind: BootstrapLlmProvisionKind
  detail: string
  providerId: LlmProviderId | null
}) {
  const copy = BOOTSTRAP_PROVISION_KIND_COPY[kind]
  // ``key_invalid`` is the only kind where a provider-specific dashboard
  // link meaningfully shortens the remediation — quota + network + 5xx
  // errors don't need the operator to mint a new key.
  const keyUrl =
    kind === "key_invalid" && providerId
      ? BOOTSTRAP_PROVIDER_KEY_URL[providerId]
      : undefined
  return (
    <div
      role="alert"
      data-testid="bootstrap-llm-provider-error"
      data-kind={kind}
      data-provider={providerId ?? ""}
      className="flex flex-col gap-1 p-3 rounded border border-[var(--destructive)] bg-[var(--destructive)]/10"
    >
      <div className="flex items-center gap-2 font-mono text-[11px] font-semibold text-[var(--destructive)]">
        <AlertCircle size={12} /> {copy.title}
      </div>
      <p className="font-mono text-[11px] text-[var(--destructive)] break-words">
        {detail}
      </p>
      <p className="font-mono text-[10px] text-[var(--muted-foreground)] leading-relaxed">
        {copy.hint}
      </p>
      {keyUrl && (
        <a
          data-testid="bootstrap-llm-provider-key-url"
          href={keyUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="self-start font-mono text-[10px] underline text-[var(--artifact-purple)] hover:opacity-80"
        >
          Open {providerId} API keys dashboard →
        </a>
      )}
    </div>
  )
}

function LlmProviderStep({
  alreadyGreen,
  onProvisioned,
}: {
  alreadyGreen: boolean
  onProvisioned: () => Promise<unknown>
}) {
  const [selected, setSelected] = useState<LlmProviderId | null>(null)
  const [apiKey, setApiKey] = useState<string>("")
  const [model, setModel] = useState<string>("")
  const [azureEndpoint, setAzureEndpoint] = useState<string>("")
  const [azureDeployment, setAzureDeployment] = useState<string>("")
  const [ollamaBaseUrl, setOllamaBaseUrl] = useState<string>("")
  const [ollamaModel, setOllamaModel] = useState<string>("")
  const [busy, setBusy] = useState(false)
  const [errKind, setErrKind] = useState<BootstrapLlmProvisionKind | null>(null)
  const [errDetail, setErrDetail] = useState<string>("")
  const [okResult, setOkResult] = useState<BootstrapLlmProvisionResponse | null>(null)

  const resetErrors = () => {
    setErrKind(null)
    setErrDetail("")
    setOkResult(null)
  }

  const canSubmit =
    !busy &&
    selected !== null &&
    (selected === "ollama"
      ? true
      : selected === "azure"
        ? apiKey.trim().length > 0 && azureEndpoint.trim().length > 0
        : apiKey.trim().length > 0)

  const handleSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault()
      if (!canSubmit || !selected) return
      setBusy(true)
      resetErrors()
      const req: BootstrapLlmProvisionRequest = { provider: selected }
      if (selected !== "ollama") req.api_key = apiKey.trim()
      const effectiveModel =
        selected === "ollama" ? ollamaModel.trim() : model.trim()
      if (effectiveModel) req.model = effectiveModel
      if (selected === "azure") {
        req.base_url = azureEndpoint.trim()
        if (azureDeployment.trim()) req.azure_deployment = azureDeployment.trim()
      }
      if (selected === "ollama" && ollamaBaseUrl.trim()) {
        req.base_url = ollamaBaseUrl.trim()
      }
      try {
        const result = await bootstrapLlmProvision(req)
        setOkResult(result)
        await onProvisioned()
      } catch (err) {
        if (err instanceof BootstrapLlmProvisionError) {
          setErrKind(err.kind)
          setErrDetail(err.detail || err.message)
        } else {
          setErrKind("provider_error")
          setErrDetail(err instanceof Error ? err.message : String(err))
        }
      } finally {
        setBusy(false)
      }
    },
    [
      canSubmit,
      selected,
      apiKey,
      model,
      ollamaModel,
      ollamaBaseUrl,
      azureEndpoint,
      azureDeployment,
      onProvisioned,
    ],
  )

  if (alreadyGreen) {
    return (
      <div
        data-testid="bootstrap-llm-provider-complete"
        className="flex flex-col gap-2 p-4 rounded border border-[var(--status-green)] bg-[var(--background)]"
      >
        <div className="flex items-center gap-2 font-mono text-xs text-[var(--status-green)]">
          <Check size={14} /> LLM provider configured
        </div>
        <p className="font-mono text-[11px] text-[var(--muted-foreground)] leading-relaxed">
          A provider has been provisioned and the gate is green. Continue
          to the next step.
        </p>
      </div>
    )
  }

  const keyLabel =
    selected === "azure"
      ? "Azure OpenAI key"
      : selected === "openai"
        ? "OpenAI API key"
        : "Anthropic API key"

  return (
    <form
      onSubmit={handleSubmit}
      data-testid="bootstrap-llm-provider-step"
      className="flex flex-col gap-3 p-4 rounded border border-[var(--border)] bg-[var(--background)]"
    >
      <div className="flex items-center gap-2 font-mono text-[10px] tracking-wider text-[var(--muted-foreground)]">
        <span>GATE</span>
        <code className="px-1.5 py-0.5 rounded bg-[var(--muted)]/50 text-[var(--foreground)]">
          llm_provider_configured === true
        </code>
      </div>
      <p className="font-mono text-[11px] text-[var(--muted-foreground)] leading-relaxed">
        Choose the LLM provider OmniSight should call for agent runs. The
        wizard runs a live <code>provider.ping()</code> before persisting
        the credential — invalid keys, exhausted quotas, and unreachable
        endpoints surface here rather than at first agent run.
      </p>

      <fieldset
        data-testid="bootstrap-llm-provider-menu"
        className="flex flex-col gap-2"
      >
        <legend className="sr-only">Select an LLM provider</legend>
        {LLM_PROVIDERS.map((p) => {
          const active = selected === p.id
          const Icon = p.icon
          return (
            <label
              key={p.id}
              data-testid={`bootstrap-llm-provider-option-${p.id}`}
              data-selected={active ? "true" : "false"}
              className={`flex items-start gap-3 p-3 rounded border cursor-pointer transition ${
                active
                  ? "border-[var(--artifact-purple)] bg-[var(--muted)]/40"
                  : "border-[var(--border)] hover:bg-[var(--muted)]/20"
              }`}
            >
              <input
                type="radio"
                name="llm-provider"
                value={p.id}
                checked={active}
                onChange={() => {
                  setSelected(p.id)
                  resetErrors()
                }}
                className="mt-1 accent-[var(--artifact-purple)]"
              />
              <span className="flex items-center justify-center w-7 h-7 rounded border border-[var(--border)] bg-[var(--background)] shrink-0">
                <Icon size={14} className="text-[var(--muted-foreground)]" />
              </span>
              <span className="flex-1 min-w-0 flex flex-col gap-0.5">
                <span className="font-mono text-xs font-semibold text-[var(--foreground)]">
                  {p.label}
                </span>
                <span className="font-mono text-[11px] text-[var(--muted-foreground)]">
                  {p.tagline}
                </span>
                <span className="font-mono text-[10px] text-[var(--muted-foreground)] italic">
                  {p.hint}
                </span>
              </span>
            </label>
          )
        })}
      </fieldset>

      {selected === "ollama" && (
        <OllamaDetectPanel
          baseUrl={ollamaBaseUrl}
          onBaseUrlChange={setOllamaBaseUrl}
          selectedModel={ollamaModel}
          onModelSelected={setOllamaModel}
        />
      )}

      {selected && selected !== "ollama" && (
        <div className="flex flex-col gap-2 p-3 rounded border border-[var(--border)] bg-[var(--background)]">
          <label className="flex flex-col gap-1">
            <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
              {keyLabel}
            </span>
            <input
              type="password"
              data-testid="bootstrap-llm-provider-api-key"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              autoComplete="off"
              placeholder={
                selected === "openai"
                  ? "sk-proj-…"
                  : selected === "anthropic"
                    ? "sk-ant-…"
                    : "•••••••••"
              }
              className="font-mono text-xs px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--card)] text-[var(--foreground)]"
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
              Model (optional — defaults to project config)
            </span>
            <input
              type="text"
              data-testid="bootstrap-llm-provider-model"
              value={model}
              onChange={(e) => setModel(e.target.value)}
              placeholder={
                selected === "openai"
                  ? "gpt-4o"
                  : selected === "anthropic"
                    ? "claude-opus-4-7"
                    : "gpt-4o"
              }
              className="font-mono text-xs px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--card)] text-[var(--foreground)]"
            />
          </label>
          {selected === "azure" && (
            <>
              <label className="flex flex-col gap-1">
                <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
                  Azure endpoint (base URL)
                </span>
                <input
                  type="text"
                  data-testid="bootstrap-llm-provider-azure-endpoint"
                  value={azureEndpoint}
                  onChange={(e) => setAzureEndpoint(e.target.value)}
                  placeholder="https://<resource>.openai.azure.com"
                  className="font-mono text-xs px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--card)] text-[var(--foreground)]"
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
                  Deployment name (optional)
                </span>
                <input
                  type="text"
                  data-testid="bootstrap-llm-provider-azure-deployment"
                  value={azureDeployment}
                  onChange={(e) => setAzureDeployment(e.target.value)}
                  placeholder="gpt-4o"
                  className="font-mono text-xs px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--card)] text-[var(--foreground)]"
                />
              </label>
            </>
          )}
        </div>
      )}

      {errKind && (
        <ProvisionErrorBanner
          kind={errKind}
          detail={errDetail}
          providerId={selected}
        />
      )}

      {okResult && (
        <div
          data-testid="bootstrap-llm-provider-success"
          className="flex flex-col gap-1 p-3 rounded border border-[var(--status-green)] bg-[var(--background)]"
        >
          <div className="flex items-center gap-2 font-mono text-[11px] font-semibold text-[var(--status-green)]">
            <Check size={12} /> Credential accepted ({okResult.latency_ms}ms)
          </div>
          <p className="font-mono text-[10px] text-[var(--muted-foreground)]">
            provider={okResult.provider} · model={okResult.model || "(default)"} ·
            fingerprint={okResult.fingerprint}
          </p>
        </div>
      )}

      <p
        data-testid="bootstrap-llm-provider-selected"
        data-value={selected ?? ""}
        data-ollama-model={selected === "ollama" ? ollamaModel : ""}
        className="font-mono text-[10px] text-[var(--muted-foreground)]"
      >
        {selected
          ? `Selected: ${selected}${
              selected === "ollama" && ollamaModel
                ? ` · model=${ollamaModel}`
                : ""
            }`
          : "No provider selected yet."}
      </p>

      <button
        type="submit"
        data-testid="bootstrap-llm-provider-submit"
        disabled={!canSubmit}
        className="self-start flex items-center gap-2 px-3 py-2 rounded bg-[var(--artifact-purple)] text-white font-mono text-xs font-semibold hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed"
      >
        {busy ? <Loader2 size={12} className="animate-spin" /> : <Shield size={12} />}
        Verify & save credential
      </button>
    </form>
  )
}

// ─── Y7 #283 — Step 2.5 (Initialize organization: tenant + super-admin) ───

function _slugifyPreview(display: string): string {
  return display
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
}

function InitTenantErrorBanner({
  kind,
  detail,
}: {
  kind: BootstrapInitTenantKind
  detail: string
}) {
  const copy = BOOTSTRAP_INIT_TENANT_KIND_COPY[kind]
  return (
    <div
      role="alert"
      data-testid="bootstrap-init-tenant-error"
      data-kind={kind}
      className="flex flex-col gap-1 p-3 rounded border border-[var(--destructive)] bg-[var(--destructive)]/10"
    >
      <div className="flex items-center gap-2 font-mono text-[11px] font-semibold text-[var(--destructive)]">
        <AlertCircle size={12} /> {copy.title}
      </div>
      <p className="font-mono text-[11px] text-[var(--destructive)] break-words">
        {detail}
      </p>
      <p className="font-mono text-[10px] text-[var(--muted-foreground)] leading-relaxed">
        {copy.hint}
      </p>
    </div>
  )
}

function InitTenantStep({
  alreadyGreen,
  onCompleted,
  onSkipped,
}: {
  alreadyGreen: boolean
  onCompleted: (resp: BootstrapInitTenantResponse) => void
  onSkipped: () => void
}) {
  const [displayName, setDisplayName] = useState("")
  const [plan, setPlan] = useState<"free" | "starter" | "pro" | "enterprise">("free")
  const [licenseKey, setLicenseKey] = useState("")
  const [adminEmail, setAdminEmail] = useState("")
  const [adminName, setAdminName] = useState("")
  const [adminPassword, setAdminPassword] = useState("")
  const [confirmPassword, setConfirmPassword] = useState("")
  const [busy, setBusy] = useState(false)
  const [errKind, setErrKind] = useState<BootstrapInitTenantKind | null>(null)
  const [errDetail, setErrDetail] = useState<string>("")
  const [localError, setLocalError] = useState<string | null>(null)
  const [okResult, setOkResult] = useState<BootstrapInitTenantResponse | null>(null)

  const slugPreview = useMemo(() => _slugifyPreview(displayName), [displayName])
  const tenantIdPreview = slugPreview ? `t-${slugPreview}` : ""

  const strength = useMemo(
    () => estimatePasswordStrength(adminPassword),
    [adminPassword],
  )
  const mismatch =
    confirmPassword.length > 0 && adminPassword !== confirmPassword
  const tooShort =
    adminPassword.length > 0 && adminPassword.length < PASSWORD_MIN_LENGTH
  const tooWeak =
    adminPassword.length >= PASSWORD_MIN_LENGTH && !strength.passes

  const canSubmit =
    !busy &&
    displayName.trim().length > 0 &&
    slugPreview.length >= 2 &&
    adminEmail.trim().length > 0 &&
    strength.passes &&
    confirmPassword === adminPassword &&
    (plan !== "enterprise" || licenseKey.trim().length >= 8)

  const handleSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault()
      if (!canSubmit) return
      setBusy(true)
      setErrKind(null)
      setErrDetail("")
      setLocalError(null)
      setOkResult(null)
      const req: BootstrapInitTenantRequest = {
        display_name: displayName.trim(),
        plan,
        admin_email: adminEmail.trim(),
        admin_password: adminPassword,
      }
      if (adminName.trim()) req.admin_name = adminName.trim()
      if (plan === "enterprise" && licenseKey.trim()) {
        req.license_key = licenseKey.trim()
      }
      try {
        const result = await bootstrapInitTenant(req)
        setOkResult(result)
        // Clear the password fields once persisted — defence in depth
        // against shoulder-surfing if the operator leaves the wizard up.
        setAdminPassword("")
        setConfirmPassword("")
        onCompleted(result)
      } catch (err) {
        if (err instanceof BootstrapInitTenantError) {
          setErrKind(err.kind)
          setErrDetail(err.detail || err.message)
        } else {
          setLocalError(err instanceof Error ? err.message : String(err))
        }
      } finally {
        setBusy(false)
      }
    },
    [
      canSubmit,
      displayName,
      plan,
      licenseKey,
      adminEmail,
      adminName,
      adminPassword,
      onCompleted,
    ],
  )

  const handleSkip = useCallback(() => {
    if (busy) return
    onSkipped()
  }, [busy, onSkipped])

  if (alreadyGreen && okResult) {
    return (
      <div
        data-testid="bootstrap-init-tenant-complete"
        className="flex flex-col gap-2 p-4 rounded border border-[var(--status-green)] bg-[var(--background)]"
      >
        <div className="flex items-center gap-2 font-mono text-xs text-[var(--status-green)]">
          <Check size={14} /> Organization initialized
        </div>
        <p className="font-mono text-[11px] text-[var(--muted-foreground)] leading-relaxed">
          Tenant <code>{okResult.tenant_id}</code> ({okResult.tenant_name},{" "}
          {okResult.plan}) is live with super-admin{" "}
          <code>{okResult.super_admin_email}</code> and default project{" "}
          <code>{okResult.project_id}</code>. Use that account to log in
          after finalize.
        </p>
        {okResult.env_write_warning && (
          <p
            data-testid="bootstrap-init-tenant-env-warning"
            className="font-mono text-[10px] text-[var(--status-yellow,#d97706)] leading-relaxed"
          >
            ⚠ {okResult.env_write_warning}
          </p>
        )}
      </div>
    )
  }

  if (alreadyGreen) {
    return (
      <div
        data-testid="bootstrap-init-tenant-skipped"
        className="flex flex-col gap-2 p-4 rounded border border-[var(--status-green)] bg-[var(--background)]"
      >
        <div className="flex items-center gap-2 font-mono text-xs text-[var(--status-green)]">
          <Check size={14} /> Step 2.5 dismissed — keeping t-default
        </div>
        <p className="font-mono text-[11px] text-[var(--muted-foreground)] leading-relaxed">
          The wizard will continue using the seeded <code>t-default</code>{" "}
          tenant and the rotated default admin. You can create more tenants
          later via <code>/admin/tenants</code>.
        </p>
      </div>
    )
  }

  return (
    <form
      onSubmit={handleSubmit}
      data-testid="bootstrap-init-tenant-form"
      className="flex flex-col gap-3 p-4 rounded border border-[var(--border)] bg-[var(--background)]"
    >
      <div className="flex items-center gap-2 font-mono text-[10px] tracking-wider text-[var(--muted-foreground)]">
        <span>OPTIONAL</span>
        <code className="px-1.5 py-0.5 rounded bg-[var(--muted)]/50 text-[var(--foreground)]">
          POST /bootstrap/init-tenant
        </code>
      </div>
      <p className="font-mono text-[11px] text-[var(--muted-foreground)] leading-relaxed">
        Create your real organization tenant now so the install lands in
        production multi-tenant shape instead of the seeded{" "}
        <code>t-default</code>. We'll create the tenant row, your first
        super-admin, an owner membership, and a default project — and pin{" "}
        <code>OMNISIGHT_PRIMARY_TENANT_ID</code> in <code>.env</code> so
        reboots default into the new tenant. Skip to keep using{" "}
        <code>t-default</code>.
      </p>

      <label className="flex flex-col gap-1">
        <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
          Organization display name
        </span>
        <input
          type="text"
          data-testid="bootstrap-init-tenant-display-name"
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
          placeholder="Acme Robotics"
          className="font-mono text-xs px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--card)] text-[var(--foreground)]"
        />
        {tenantIdPreview && (
          <span
            data-testid="bootstrap-init-tenant-slug-preview"
            className="font-mono text-[10px] text-[var(--muted-foreground)]"
          >
            tenant id will be <code>{tenantIdPreview}</code>
          </span>
        )}
      </label>

      <label className="flex flex-col gap-1">
        <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
          Plan
        </span>
        <select
          data-testid="bootstrap-init-tenant-plan"
          value={plan}
          onChange={(e) =>
            setPlan(e.target.value as "free" | "starter" | "pro" | "enterprise")
          }
          className="font-mono text-xs px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--card)] text-[var(--foreground)]"
        >
          <option value="free">free (default)</option>
          <option value="starter">starter</option>
          <option value="pro">pro</option>
          <option value="enterprise">enterprise (license required)</option>
        </select>
      </label>

      {plan === "enterprise" && (
        <label className="flex flex-col gap-1">
          <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
            Enterprise license key
          </span>
          <input
            type="text"
            data-testid="bootstrap-init-tenant-license-key"
            value={licenseKey}
            onChange={(e) => setLicenseKey(e.target.value)}
            placeholder="OMNI-XXXXXXXX-XXXXXXXX"
            className="font-mono text-xs px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--card)] text-[var(--foreground)]"
          />
          <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
            8-256 chars, letters / digits / dash / underscore.
          </span>
        </label>
      )}

      <label className="flex flex-col gap-1">
        <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
          Super-admin email
        </span>
        <input
          type="email"
          data-testid="bootstrap-init-tenant-admin-email"
          value={adminEmail}
          onChange={(e) => setAdminEmail(e.target.value)}
          placeholder="founder@acme.example"
          autoComplete="email"
          className="font-mono text-xs px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--card)] text-[var(--foreground)]"
        />
      </label>

      <label className="flex flex-col gap-1">
        <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
          Super-admin name (optional)
        </span>
        <input
          type="text"
          data-testid="bootstrap-init-tenant-admin-name"
          value={adminName}
          onChange={(e) => setAdminName(e.target.value)}
          placeholder="Defaults to organization name"
          className="font-mono text-xs px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--card)] text-[var(--foreground)]"
        />
      </label>

      <label className="flex flex-col gap-1">
        <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
          Super-admin password (min 12 chars, zxcvbn ≥ {PASSWORD_MIN_SCORE})
        </span>
        <input
          type="password"
          data-testid="bootstrap-init-tenant-admin-password"
          value={adminPassword}
          onChange={(e) => setAdminPassword(e.target.value)}
          autoComplete="new-password"
          minLength={12}
          className="font-mono text-xs px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--card)] text-[var(--foreground)]"
        />
      </label>

      <label className="flex flex-col gap-1">
        <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
          Confirm password
        </span>
        <input
          type="password"
          data-testid="bootstrap-init-tenant-admin-password-confirm"
          value={confirmPassword}
          onChange={(e) => setConfirmPassword(e.target.value)}
          autoComplete="new-password"
          minLength={12}
          className="font-mono text-xs px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--card)] text-[var(--foreground)]"
        />
      </label>

      {adminPassword.length > 0 && (
        <div
          data-testid="bootstrap-init-tenant-strength"
          data-score={strength.score}
          data-passes={strength.passes ? "true" : "false"}
          className="flex items-center gap-2"
        >
          <div className="flex gap-1 flex-1" aria-hidden>
            {[0, 1, 2, 3, 4].map((i) => (
              <span
                key={i}
                className={`h-1.5 flex-1 rounded ${
                  i < strength.score
                    ? strength.passes
                      ? "bg-[var(--status-green)]"
                      : "bg-[var(--status-yellow,#d97706)]"
                    : "bg-[var(--muted)]"
                }`}
              />
            ))}
          </div>
          <span
            className={`font-mono text-[10px] uppercase tracking-wider ${
              strength.passes
                ? "text-[var(--status-green)]"
                : "text-[var(--muted-foreground)]"
            }`}
          >
            {strength.label} · {strength.score}/4
          </span>
        </div>
      )}
      {tooShort && (
        <p className="font-mono text-[11px] text-[var(--destructive)]">
          Password must be at least {PASSWORD_MIN_LENGTH} characters.
        </p>
      )}
      {tooWeak && !tooShort && (
        <p className="font-mono text-[11px] text-[var(--destructive)]">
          Password is too guessable — score ≥ {PASSWORD_MIN_SCORE} required.
        </p>
      )}
      {mismatch && (
        <p className="font-mono text-[11px] text-[var(--destructive)]">
          Password and confirmation do not match.
        </p>
      )}
      {errKind && <InitTenantErrorBanner kind={errKind} detail={errDetail} />}
      {!errKind && localError && (
        <p
          role="alert"
          data-testid="bootstrap-init-tenant-error"
          data-kind="unclassified"
          className="font-mono text-[11px] text-[var(--destructive)] break-words"
        >
          {localError}
        </p>
      )}

      <div className="flex flex-wrap items-center gap-2 pt-1">
        <button
          type="submit"
          data-testid="bootstrap-init-tenant-submit"
          disabled={!canSubmit}
          className="flex items-center gap-2 px-3 py-2 rounded bg-[var(--artifact-purple)] text-white font-mono text-xs font-semibold hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {busy ? <Loader2 size={12} className="animate-spin" /> : <Database size={12} />}
          Create tenant + super-admin
        </button>
        <button
          type="button"
          data-testid="bootstrap-init-tenant-skip"
          onClick={handleSkip}
          disabled={busy}
          className="flex items-center gap-2 px-3 py-2 rounded border border-[var(--border)] font-mono text-xs hover:bg-[var(--muted)]/40 disabled:opacity-40"
        >
          Skip — keep t-default
        </button>
      </div>
    </form>
  )
}

// ─── L4 — Step 3 (Cloudflare Tunnel: embed B12 wizard + LAN-only skip) ──

function CfTunnelStep({
  alreadyGreen,
  onChanged,
}: {
  alreadyGreen: boolean
  onChanged: () => Promise<unknown>
}) {
  const [wizardOpen, setWizardOpen] = useState(false)
  const [skipBusy, setSkipBusy] = useState(false)
  const [skipError, setSkipError] = useState<string | null>(null)
  const [skipReason, setSkipReason] = useState<string>("")
  const [showSkipForm, setShowSkipForm] = useState(false)

  const handleWizardClose = useCallback(async () => {
    setWizardOpen(false)
    // After the operator closes the B12 modal (success or cancel), pull
    // the bootstrap gate status so a successful provision flips the
    // step to green without a manual refresh.
    await onChanged()
  }, [onChanged])

  const handleSkip = useCallback(async () => {
    setSkipBusy(true)
    setSkipError(null)
    try {
      await bootstrapCfTunnelSkip(skipReason.trim())
      await onChanged()
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      setSkipError(msg)
    } finally {
      setSkipBusy(false)
    }
  }, [skipReason, onChanged])

  if (alreadyGreen) {
    return (
      <div
        data-testid="bootstrap-cf-tunnel-complete"
        className="flex flex-col gap-2 p-4 rounded border border-[var(--status-green)] bg-[var(--background)]"
      >
        <div className="flex items-center gap-2 font-mono text-xs text-[var(--status-green)]">
          <Check size={14} /> Cloudflare Tunnel step complete
        </div>
        <p className="font-mono text-[11px] text-[var(--muted-foreground)] leading-relaxed">
          Either a tunnel has been provisioned, or the operator opted for
          a LAN-only deployment. Continue to the next step.
        </p>
      </div>
    )
  }

  return (
    <div
      data-testid="bootstrap-cf-tunnel-step"
      className="flex flex-col gap-3 p-4 rounded border border-[var(--border)] bg-[var(--background)]"
    >
      <div className="flex items-center gap-2 font-mono text-[10px] tracking-wider text-[var(--muted-foreground)]">
        <span>GATE</span>
        <code className="px-1.5 py-0.5 rounded bg-[var(--muted)]/50 text-[var(--foreground)]">
          cf_tunnel_configured === true
        </code>
      </div>
      <p className="font-mono text-[11px] text-[var(--muted-foreground)] leading-relaxed">
        Expose this install on the public internet through a Cloudflare
        Tunnel (B12 wizard), or skip for a LAN-only deployment. The
        B12 wizard writes <code>bootstrap_state.cf_tunnel_configured</code>
        automatically once it provisions a tunnel; skip records an
        audit warning so the choice is traceable.
      </p>

      <button
        type="button"
        data-testid="bootstrap-cf-tunnel-launch"
        onClick={() => setWizardOpen(true)}
        className="self-start flex items-center gap-2 px-3 py-2 rounded bg-[var(--artifact-purple)] text-white font-mono text-xs font-semibold hover:opacity-90"
      >
        <Cloud size={12} />
        Configure Cloudflare Tunnel…
      </button>

      <div className="flex flex-col gap-2 p-3 rounded border border-dashed border-[var(--border)]">
        <div className="font-mono text-[10px] tracking-wider text-[var(--muted-foreground)]">
          LAN-ONLY
        </div>
        <p className="font-mono text-[11px] text-[var(--muted-foreground)] leading-relaxed">
          If this install will only be reached from the local network,
          you can skip the tunnel. An <code>audit_log</code> row is
          written with warning severity so the choice is on record.
        </p>
        {!showSkipForm ? (
          <button
            type="button"
            data-testid="bootstrap-cf-tunnel-skip-reveal"
            onClick={() => setShowSkipForm(true)}
            className="self-start font-mono text-[11px] px-2 py-1 rounded border border-[var(--border)] hover:bg-[var(--muted)]/40"
          >
            Skip (LAN-only)
          </button>
        ) : (
          <div className="flex flex-col gap-2">
            <label className="flex flex-col gap-1">
              <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
                Reason (optional — shown in audit log)
              </span>
              <input
                type="text"
                data-testid="bootstrap-cf-tunnel-skip-reason"
                value={skipReason}
                onChange={(e) => setSkipReason(e.target.value)}
                placeholder="e.g. air-gapped lab install"
                maxLength={500}
                className="font-mono text-xs px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--card)] text-[var(--foreground)]"
              />
            </label>
            {skipError && (
              <p
                role="alert"
                data-testid="bootstrap-cf-tunnel-skip-error"
                className="font-mono text-[11px] text-[var(--destructive)] break-words"
              >
                {skipError}
              </p>
            )}
            <div className="flex items-center gap-2">
              <button
                type="button"
                data-testid="bootstrap-cf-tunnel-skip-confirm"
                onClick={() => void handleSkip()}
                disabled={skipBusy}
                className="flex items-center gap-2 font-mono text-[11px] px-2 py-1 rounded border border-[var(--destructive)] text-[var(--destructive)] hover:bg-[var(--destructive)]/10 disabled:opacity-40"
              >
                {skipBusy ? (
                  <Loader2 size={12} className="animate-spin" />
                ) : (
                  <AlertCircle size={12} />
                )}
                Confirm skip
              </button>
              <button
                type="button"
                onClick={() => {
                  setShowSkipForm(false)
                  setSkipError(null)
                }}
                disabled={skipBusy}
                className="font-mono text-[11px] px-2 py-1 rounded border border-[var(--border)] hover:bg-[var(--muted)]/40 disabled:opacity-40"
              >
                Cancel
              </button>
            </div>
          </div>
        )}
      </div>

      <CloudflareTunnelSetup open={wizardOpen} onClose={handleWizardClose} />
    </div>
  )
}

// ─── B14 Part A — Step 3.5 (Git Forge setup, optional) ───────────────
//
// Slot for per-forge (GitHub / GitLab / Gerrit) configuration during
// first-install. Intentionally optional — `finalizeBootstrap` does not
// block on Git forge credentials, so this step is gated on local state
// (`localGreen.git_forge`) rather than a backend gate flag.
//
// This row introduces the three-way tab shell (GitHub / GitLab /
// Gerrit) + an explicit "Skip — configure later" button. The GitHub
// tab wires a token input + Test Connection button against
// `POST /system/test/git-forge-token` (a non-mutating probe — it does
// NOT write the candidate token into `settings.github_token`; that
// happens on explicit Save). GitLab / Gerrit remain placeholders
// pending follow-up B14 rows. Both the skip button and a successful
// GitHub Save flip `localGreen.git_forge=true` so the auto-advance
// moves on.

type GitForgeTab = "github" | "gitlab" | "gerrit"

const GIT_FORGE_TABS: { id: GitForgeTab; label: string; hint: string }[] = [
  {
    id: "github",
    label: "GitHub",
    hint: "Personal Access Token with repo + pull_request scopes",
  },
  {
    id: "gitlab",
    label: "GitLab",
    hint: "Self-hosted URL + Personal Access Token (api scope)",
  },
  {
    id: "gerrit",
    label: "Gerrit",
    hint: "REST URL + SSH host/port for the merger-agent-bot account",
  },
]

function GitForgeStep({
  alreadyGreen,
  onCompleted,
}: {
  alreadyGreen: boolean
  onCompleted: () => void
}) {
  const [activeTab, setActiveTab] = useState<GitForgeTab>("github")
  const activeDef = GIT_FORGE_TABS.find((t) => t.id === activeTab) ?? GIT_FORGE_TABS[0]

  return (
    <div
      data-testid="bootstrap-git-forge-step"
      data-already-green={alreadyGreen ? "true" : "false"}
      className="flex flex-col gap-3 p-4 rounded border border-[var(--border)] bg-[var(--background)]"
    >
      {alreadyGreen && (
        <div
          data-testid="bootstrap-git-forge-complete"
          className="flex items-center gap-2 p-2 rounded border border-[var(--status-green)] bg-[var(--status-green)]/10 font-mono text-[11px] text-[var(--status-green)]"
        >
          <Check size={12} />
          Marked complete — skip applied. Switch tabs below to revisit setup,
          or continue to the next step.
        </div>
      )}
      <div className="flex items-center gap-2 font-mono text-[10px] tracking-wider text-[var(--muted-foreground)]">
        <span>OPTIONAL</span>
        <code className="px-1.5 py-0.5 rounded bg-[var(--muted)]/50 text-[var(--foreground)]">
          not a finalize gate
        </code>
      </div>
      <p className="font-mono text-[11px] text-[var(--muted-foreground)] leading-relaxed">
        Connect OmniSight to one or more Git forges (GitHub / GitLab /
        Gerrit) so the review + merge agents can act on patches. You can
        set this up now or skip and configure it later under
        <code className="mx-1">Settings → Integration</code>. Skipping does
        not block finalize.
      </p>

      <div
        data-testid="bootstrap-git-forge-tabs"
        role="tablist"
        aria-label="Git forge provider"
        className="flex rounded border border-[var(--border)] overflow-hidden"
      >
        {GIT_FORGE_TABS.map((tab) => {
          const selected = activeTab === tab.id
          return (
            <button
              key={tab.id}
              type="button"
              role="tab"
              aria-selected={selected}
              aria-controls={`bootstrap-git-forge-panel-${tab.id}`}
              id={`bootstrap-git-forge-tab-${tab.id}`}
              data-testid={`bootstrap-git-forge-tab-${tab.id}`}
              data-active={selected}
              onClick={() => setActiveTab(tab.id)}
              className={`flex-1 px-3 py-2 font-mono text-[11px] font-semibold transition ${
                selected
                  ? "bg-[var(--artifact-purple)] text-white"
                  : "bg-[var(--background)] text-[var(--muted-foreground)] hover:bg-[var(--muted)]/40"
              }`}
            >
              {tab.label}
            </button>
          )
        })}
      </div>

      <div
        role="tabpanel"
        id={`bootstrap-git-forge-panel-${activeDef.id}`}
        aria-labelledby={`bootstrap-git-forge-tab-${activeDef.id}`}
        data-testid={`bootstrap-git-forge-panel-${activeDef.id}`}
        className="flex flex-col gap-2 p-3 rounded border border-dashed border-[var(--border)] bg-[var(--muted)]/20"
      >
        <div className="flex items-center gap-2 font-mono text-[10px] tracking-wider text-[var(--muted-foreground)]">
          <GitBranch size={12} />
          <span>{activeDef.label.toUpperCase()} PROVIDER</span>
        </div>
        <p className="font-mono text-[11px] text-[var(--foreground)] leading-relaxed">
          {activeDef.hint}
        </p>
        {activeTab === "github" ? (
          <GitHubTokenForm onSaved={onCompleted} />
        ) : activeTab === "gitlab" ? (
          <GitLabTokenForm onSaved={onCompleted} />
        ) : (
          <GerritSshForm onSaved={onCompleted} />
        )}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          data-testid="bootstrap-git-forge-skip"
          onClick={onCompleted}
          className="flex items-center gap-2 px-3 py-2 rounded bg-[var(--artifact-purple)] text-white font-mono text-xs font-semibold hover:opacity-90"
        >
          <Check size={12} />
          Skip — configure later in Settings → Integration
        </button>
      </div>
    </div>
  )
}

// ─── B14 Part A row 3: GitHub token input + Test Connection ──────────
//
// Lives inside the Step 3.5 "github" tab panel. The operator pastes a
// Personal Access Token, hits "Test Connection", and the form calls
// `testGitForgeToken` — a non-mutating probe against
// `POST /system/git-forge/test-token` that hits GitHub's `/user` with
// the candidate token and returns `{status, user, name, scopes}`.
// Nothing is persisted until the operator explicitly clicks
// "Save & Continue", which writes `github_token` via `updateSettings`
// and flips `localGreen.git_forge=true` through `onSaved`.
//
// Keeping test and save as two distinct actions means a bad token can
// never land in `settings.github_token` — a regression we'd pay for
// later when the merge + review agents pick it up.

function GitHubTokenForm({ onSaved }: { onSaved: () => void }) {
  const [token, setToken] = useState("")
  const [busy, setBusy] = useState(false)
  const [saving, setSaving] = useState(false)
  const [result, setResult] = useState<GitForgeTokenTestResult | null>(null)
  const [saveError, setSaveError] = useState<string | null>(null)

  const onTest = useCallback(async () => {
    const trimmed = token.trim()
    if (!trimmed || busy) return
    setBusy(true)
    setResult(null)
    setSaveError(null)
    try {
      const res = await testGitForgeToken({ provider: "github", token: trimmed })
      setResult(res)
    } catch (err) {
      setResult({
        status: "error",
        message:
          err instanceof Error
            ? err.message
            : "Failed to reach the GitHub API probe",
      })
    } finally {
      setBusy(false)
    }
  }, [token, busy])

  const onSave = useCallback(async () => {
    if (saving || result?.status !== "ok") return
    setSaving(true)
    setSaveError(null)
    try {
      await updateSettings({ github_token: token.trim() })
      onSaved()
    } catch (err) {
      setSaveError(
        err instanceof Error ? err.message : "Failed to save the token",
      )
    } finally {
      setSaving(false)
    }
  }, [token, saving, result, onSaved])

  const canTest = token.trim().length > 0 && !busy
  const ok = result?.status === "ok"

  return (
    <div className="flex flex-col gap-2" data-testid="bootstrap-git-forge-github-form">
      <label
        htmlFor="bootstrap-git-forge-github-token"
        className="font-mono text-[10px] tracking-wider text-[var(--muted-foreground)]"
      >
        PERSONAL ACCESS TOKEN
      </label>
      <input
        id="bootstrap-git-forge-github-token"
        data-testid="bootstrap-git-forge-github-token"
        type="password"
        autoComplete="off"
        spellCheck={false}
        value={token}
        onChange={(e) => {
          setToken(e.target.value)
          setResult(null)
          setSaveError(null)
        }}
        placeholder="ghp_... (classic) or github_pat_... (fine-grained)"
        className="font-mono text-[11px] px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--background)] focus:outline-none focus:border-[var(--artifact-purple)]"
      />
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          data-testid="bootstrap-git-forge-github-test"
          onClick={onTest}
          disabled={!canTest}
          className="flex items-center gap-2 px-3 py-1.5 rounded border border-[var(--border)] bg-[var(--background)] font-mono text-[11px] font-semibold hover:bg-[var(--muted)]/40 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {busy ? <Loader2 size={12} className="animate-spin" /> : <Activity size={12} />}
          {busy ? "Testing…" : "Test Connection"}
        </button>
        {ok && (
          <button
            type="button"
            data-testid="bootstrap-git-forge-github-save"
            onClick={onSave}
            disabled={saving}
            className="flex items-center gap-2 px-3 py-1.5 rounded bg-[var(--artifact-purple)] text-white font-mono text-[11px] font-semibold hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {saving ? <Loader2 size={12} className="animate-spin" /> : <Check size={12} />}
            {saving ? "Saving…" : "Save & Continue"}
          </button>
        )}
      </div>
      {result && (
        <div
          data-testid="bootstrap-git-forge-github-result"
          data-status={result.status}
          className={`flex items-start gap-2 p-2 rounded border font-mono text-[11px] ${
            ok
              ? "border-[var(--status-green)] bg-[var(--status-green)]/10 text-[var(--status-green)]"
              : "border-[var(--status-red)] bg-[var(--status-red)]/10 text-[var(--status-red)]"
          }`}
        >
          {ok ? <Check size={12} className="mt-0.5" /> : <AlertCircle size={12} className="mt-0.5" />}
          {ok ? (
            <div className="flex flex-col gap-0.5">
              <span>
                Connected as{" "}
                <strong data-testid="bootstrap-git-forge-github-user">
                  {result.user}
                </strong>
                {result.name && result.name !== result.user ? ` (${result.name})` : ""}
              </span>
              {result.scopes ? (
                <span className="text-[10px] opacity-80">
                  Scopes: <code>{result.scopes}</code>
                </span>
              ) : null}
            </div>
          ) : (
            <span>{result.message || "GitHub API rejected the token"}</span>
          )}
        </div>
      )}
      {saveError && (
        <div
          data-testid="bootstrap-git-forge-github-save-error"
          className="flex items-start gap-2 p-2 rounded border border-[var(--status-red)] bg-[var(--status-red)]/10 font-mono text-[11px] text-[var(--status-red)]"
        >
          <AlertCircle size={12} className="mt-0.5" />
          <span>{saveError}</span>
        </div>
      )}
    </div>
  )
}

// ─── B14 Part A row 4: GitLab URL + token + Test Connection ──────────
//
// Lives inside the Step 3.5 "gitlab" tab panel. Mirrors the GitHub form
// but adds a URL field for self-hosted instances (defaults to
// `https://gitlab.com` if blank). Test Connection calls
// `testGitForgeToken({ provider: "gitlab", url, token })` which the
// backend routes to `_probe_gitlab_token` → `GET {url}/api/v4/version`.
// On success the GitLab instance version surfaces so the operator can
// verify they pasted the right URL / token against the right server
// before Save & Continue persists both `gitlab_token` and `gitlab_url`.
//
// As with the GitHub form, test and save are two distinct actions so a
// bad token can never land in `settings.gitlab_token` — the release +
// issue-tracker paths read that field directly.

function GitLabTokenForm({ onSaved }: { onSaved: () => void }) {
  const [url, setUrl] = useState("")
  const [token, setToken] = useState("")
  const [busy, setBusy] = useState(false)
  const [saving, setSaving] = useState(false)
  const [result, setResult] = useState<GitForgeTokenTestResult | null>(null)
  const [saveError, setSaveError] = useState<string | null>(null)

  const onTest = useCallback(async () => {
    const trimmedToken = token.trim()
    if (!trimmedToken || busy) return
    setBusy(true)
    setResult(null)
    setSaveError(null)
    try {
      const res = await testGitForgeToken({
        provider: "gitlab",
        token: trimmedToken,
        url: url.trim(),
      })
      setResult(res)
    } catch (err) {
      setResult({
        status: "error",
        message:
          err instanceof Error
            ? err.message
            : "Failed to reach the GitLab API probe",
      })
    } finally {
      setBusy(false)
    }
  }, [token, url, busy])

  const onSave = useCallback(async () => {
    if (saving || result?.status !== "ok") return
    setSaving(true)
    setSaveError(null)
    try {
      // The probe returns the *effective* URL (falls back to
      // https://gitlab.com when the operator leaves the field blank) —
      // persist that so later reads of `settings.gitlab_url` match
      // what was actually validated.
      await updateSettings({
        gitlab_token: token.trim(),
        gitlab_url: result.url ?? url.trim(),
      })
      onSaved()
    } catch (err) {
      setSaveError(
        err instanceof Error ? err.message : "Failed to save the token",
      )
    } finally {
      setSaving(false)
    }
  }, [token, url, saving, result, onSaved])

  const canTest = token.trim().length > 0 && !busy
  const ok = result?.status === "ok"

  return (
    <div className="flex flex-col gap-2" data-testid="bootstrap-git-forge-gitlab-form">
      <label
        htmlFor="bootstrap-git-forge-gitlab-url"
        className="font-mono text-[10px] tracking-wider text-[var(--muted-foreground)]"
      >
        INSTANCE URL (optional — defaults to https://gitlab.com)
      </label>
      <input
        id="bootstrap-git-forge-gitlab-url"
        data-testid="bootstrap-git-forge-gitlab-url"
        type="text"
        autoComplete="off"
        spellCheck={false}
        value={url}
        onChange={(e) => {
          setUrl(e.target.value)
          setResult(null)
          setSaveError(null)
        }}
        placeholder="https://gitlab.example.com"
        className="font-mono text-[11px] px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--background)] focus:outline-none focus:border-[var(--artifact-purple)]"
      />
      <label
        htmlFor="bootstrap-git-forge-gitlab-token"
        className="font-mono text-[10px] tracking-wider text-[var(--muted-foreground)]"
      >
        PERSONAL ACCESS TOKEN (api scope)
      </label>
      <input
        id="bootstrap-git-forge-gitlab-token"
        data-testid="bootstrap-git-forge-gitlab-token"
        type="password"
        autoComplete="off"
        spellCheck={false}
        value={token}
        onChange={(e) => {
          setToken(e.target.value)
          setResult(null)
          setSaveError(null)
        }}
        placeholder="glpat-..."
        className="font-mono text-[11px] px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--background)] focus:outline-none focus:border-[var(--artifact-purple)]"
      />
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          data-testid="bootstrap-git-forge-gitlab-test"
          onClick={onTest}
          disabled={!canTest}
          className="flex items-center gap-2 px-3 py-1.5 rounded border border-[var(--border)] bg-[var(--background)] font-mono text-[11px] font-semibold hover:bg-[var(--muted)]/40 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {busy ? <Loader2 size={12} className="animate-spin" /> : <Activity size={12} />}
          {busy ? "Testing…" : "Test Connection"}
        </button>
        {ok && (
          <button
            type="button"
            data-testid="bootstrap-git-forge-gitlab-save"
            onClick={onSave}
            disabled={saving}
            className="flex items-center gap-2 px-3 py-1.5 rounded bg-[var(--artifact-purple)] text-white font-mono text-[11px] font-semibold hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {saving ? <Loader2 size={12} className="animate-spin" /> : <Check size={12} />}
            {saving ? "Saving…" : "Save & Continue"}
          </button>
        )}
      </div>
      {result && (
        <div
          data-testid="bootstrap-git-forge-gitlab-result"
          data-status={result.status}
          className={`flex items-start gap-2 p-2 rounded border font-mono text-[11px] ${
            ok
              ? "border-[var(--status-green)] bg-[var(--status-green)]/10 text-[var(--status-green)]"
              : "border-[var(--status-red)] bg-[var(--status-red)]/10 text-[var(--status-red)]"
          }`}
        >
          {ok ? <Check size={12} className="mt-0.5" /> : <AlertCircle size={12} className="mt-0.5" />}
          {ok ? (
            <div className="flex flex-col gap-0.5">
              <span>
                GitLab{" "}
                <strong data-testid="bootstrap-git-forge-gitlab-version">
                  {result.version}
                </strong>
                {result.revision ? ` (${result.revision})` : ""}
              </span>
              {result.url ? (
                <span className="text-[10px] opacity-80">
                  Instance: <code>{result.url}</code>
                </span>
              ) : null}
            </div>
          ) : (
            <span>{result.message || "GitLab API rejected the token"}</span>
          )}
        </div>
      )}
      {saveError && (
        <div
          data-testid="bootstrap-git-forge-gitlab-save-error"
          className="flex items-start gap-2 p-2 rounded border border-[var(--status-red)] bg-[var(--status-red)]/10 font-mono text-[11px] text-[var(--status-red)]"
        >
          <AlertCircle size={12} className="mt-0.5" />
          <span>{saveError}</span>
        </div>
      )}
    </div>
  )
}

// ─── B14 Part A row 5: Gerrit URL + SSH host/port + Test SSH ─────────
//
// Lives inside the Step 3.5 "gerrit" tab panel. Unlike GitHub/GitLab
// the credential under test here is an SSH reachability check, not a
// token: Gerrit's first-class transport is SSH, and the merger agent
// will later drive `gerrit review` / `gerrit version` over the same
// endpoint. "Test SSH" calls `testGitForgeToken({ provider: "gerrit",
// ssh_host, ssh_port, url })` which the backend routes to
// `_probe_gerrit_ssh` → `ssh -p {port} {host} gerrit version`.
//
// On success the Gerrit version surfaces so the operator can confirm
// they are reaching the right instance before Save & Continue persists
// `gerrit_enabled`, `gerrit_url`, `gerrit_ssh_host`, and `gerrit_ssh_port`.
// Same two-step test/save split as the token-based forms — SSH failures
// never leave a half-configured Gerrit endpoint in `settings.*`.

const DEFAULT_GERRIT_SSH_PORT = 29418

function GerritSshForm({ onSaved }: { onSaved: () => void }) {
  const [url, setUrl] = useState("")
  const [sshHost, setSshHost] = useState("")
  const [sshPort, setSshPort] = useState<string>(String(DEFAULT_GERRIT_SSH_PORT))
  const [busy, setBusy] = useState(false)
  const [saving, setSaving] = useState(false)
  const [result, setResult] = useState<GitForgeTokenTestResult | null>(null)
  const [saveError, setSaveError] = useState<string | null>(null)

  const parsedPort = (() => {
    const trimmed = sshPort.trim()
    if (!trimmed) return DEFAULT_GERRIT_SSH_PORT
    const n = Number(trimmed)
    return Number.isFinite(n) && Number.isInteger(n) ? n : NaN
  })()
  const portValid =
    Number.isInteger(parsedPort) && parsedPort >= 1 && parsedPort <= 65535
  const canTest = sshHost.trim().length > 0 && portValid && !busy

  const onTest = useCallback(async () => {
    if (!canTest) return
    setBusy(true)
    setResult(null)
    setSaveError(null)
    try {
      const res = await testGitForgeToken({
        provider: "gerrit",
        ssh_host: sshHost.trim(),
        ssh_port: parsedPort as number,
        url: url.trim(),
      })
      setResult(res)
    } catch (err) {
      setResult({
        status: "error",
        message:
          err instanceof Error
            ? err.message
            : "Failed to reach the Gerrit SSH probe",
      })
    } finally {
      setBusy(false)
    }
  }, [sshHost, parsedPort, url, canTest])

  const onSave = useCallback(async () => {
    if (saving || result?.status !== "ok") return
    setSaving(true)
    setSaveError(null)
    try {
      await updateSettings({
        gerrit_enabled: true,
        gerrit_url: (result.url ?? url.trim()) || "",
        gerrit_ssh_host: result.ssh_host ?? sshHost.trim(),
        gerrit_ssh_port: result.ssh_port ?? (parsedPort as number),
      })
      onSaved()
    } catch (err) {
      setSaveError(
        err instanceof Error ? err.message : "Failed to save Gerrit settings",
      )
    } finally {
      setSaving(false)
    }
  }, [sshHost, url, parsedPort, saving, result, onSaved])

  const ok = result?.status === "ok"

  return (
    <div className="flex flex-col gap-2" data-testid="bootstrap-git-forge-gerrit-form">
      <label
        htmlFor="bootstrap-git-forge-gerrit-url"
        className="font-mono text-[10px] tracking-wider text-[var(--muted-foreground)]"
      >
        REST URL (optional — used later for webhooks / submit-rule checks)
      </label>
      <input
        id="bootstrap-git-forge-gerrit-url"
        data-testid="bootstrap-git-forge-gerrit-url"
        type="text"
        autoComplete="off"
        spellCheck={false}
        value={url}
        onChange={(e) => {
          setUrl(e.target.value)
          setResult(null)
          setSaveError(null)
        }}
        placeholder="https://gerrit.example.com"
        className="font-mono text-[11px] px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--background)] focus:outline-none focus:border-[var(--artifact-purple)]"
      />
      <label
        htmlFor="bootstrap-git-forge-gerrit-ssh-host"
        className="font-mono text-[10px] tracking-wider text-[var(--muted-foreground)]"
      >
        SSH HOST ([user@]host — merger-agent-bot account recommended)
      </label>
      <input
        id="bootstrap-git-forge-gerrit-ssh-host"
        data-testid="bootstrap-git-forge-gerrit-ssh-host"
        type="text"
        autoComplete="off"
        spellCheck={false}
        value={sshHost}
        onChange={(e) => {
          setSshHost(e.target.value)
          setResult(null)
          setSaveError(null)
        }}
        placeholder="merger-agent-bot@gerrit.example.com"
        className="font-mono text-[11px] px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--background)] focus:outline-none focus:border-[var(--artifact-purple)]"
      />
      <label
        htmlFor="bootstrap-git-forge-gerrit-ssh-port"
        className="font-mono text-[10px] tracking-wider text-[var(--muted-foreground)]"
      >
        SSH PORT (Gerrit default: {DEFAULT_GERRIT_SSH_PORT})
      </label>
      <input
        id="bootstrap-git-forge-gerrit-ssh-port"
        data-testid="bootstrap-git-forge-gerrit-ssh-port"
        type="number"
        min={1}
        max={65535}
        step={1}
        autoComplete="off"
        spellCheck={false}
        value={sshPort}
        onChange={(e) => {
          setSshPort(e.target.value)
          setResult(null)
          setSaveError(null)
        }}
        placeholder={String(DEFAULT_GERRIT_SSH_PORT)}
        className="font-mono text-[11px] px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--background)] focus:outline-none focus:border-[var(--artifact-purple)] w-32"
      />
      {!portValid && sshPort.trim().length > 0 && (
        <div
          data-testid="bootstrap-git-forge-gerrit-port-invalid"
          className="font-mono text-[10px] text-[var(--status-red)]"
        >
          Port must be an integer between 1 and 65535.
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          data-testid="bootstrap-git-forge-gerrit-test"
          onClick={onTest}
          disabled={!canTest}
          className="flex items-center gap-2 px-3 py-1.5 rounded border border-[var(--border)] bg-[var(--background)] font-mono text-[11px] font-semibold hover:bg-[var(--muted)]/40 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {busy ? <Loader2 size={12} className="animate-spin" /> : <Activity size={12} />}
          {busy ? "Testing…" : "Test SSH"}
        </button>
        {ok && (
          <button
            type="button"
            data-testid="bootstrap-git-forge-gerrit-save"
            onClick={onSave}
            disabled={saving}
            className="flex items-center gap-2 px-3 py-1.5 rounded bg-[var(--artifact-purple)] text-white font-mono text-[11px] font-semibold hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {saving ? <Loader2 size={12} className="animate-spin" /> : <Check size={12} />}
            {saving ? "Saving…" : "Save & Continue"}
          </button>
        )}
      </div>
      {result && (
        <div
          data-testid="bootstrap-git-forge-gerrit-result"
          data-status={result.status}
          className={`flex items-start gap-2 p-2 rounded border font-mono text-[11px] ${
            ok
              ? "border-[var(--status-green)] bg-[var(--status-green)]/10 text-[var(--status-green)]"
              : "border-[var(--status-red)] bg-[var(--status-red)]/10 text-[var(--status-red)]"
          }`}
        >
          {ok ? <Check size={12} className="mt-0.5" /> : <AlertCircle size={12} className="mt-0.5" />}
          {ok ? (
            <div className="flex flex-col gap-0.5">
              <span>
                Gerrit{" "}
                <strong data-testid="bootstrap-git-forge-gerrit-version">
                  {result.version}
                </strong>
              </span>
              {result.ssh_host ? (
                <span className="text-[10px] opacity-80">
                  SSH: <code>{result.ssh_host}:{result.ssh_port}</code>
                </span>
              ) : null}
            </div>
          ) : (
            <span>{result.message || "Gerrit SSH probe failed"}</span>
          )}
        </div>
      )}
      {saveError && (
        <div
          data-testid="bootstrap-git-forge-gerrit-save-error"
          className="flex items-start gap-2 p-2 rounded border border-[var(--status-red)] bg-[var(--status-red)]/10 font-mono text-[11px] text-[var(--status-red)]"
        >
          <AlertCircle size={12} className="mt-0.5" />
          <span>{saveError}</span>
        </div>
      )}
    </div>
  )
}

// ─── BS.9.2 / BS.9.5 — Step 5.5 (Vertical setup, optional) ──────────
//
// Wizard-shell row matching backend ``STEP_VERTICAL_SETUP`` (see
// ``backend/bootstrap.py`` and ``alembic/0054_bootstrap_state_metadata_jsonb``).
// The step is *optional* — finalize never blocks on it, and existing
// installs that finalized pre-BS.9 auto-redirect away from
// ``/bootstrap`` before this pill ever renders, so they never see it.
//
// BS.9.2 introduced the frame; BS.9.3 plugged the multi-pick chips
// in; BS.9.4 added the AndroidApiSelector; BS.9.5 (this row) wires
// Confirm picks to (1) batch ``POST /installer/jobs`` (one job per
// selected vertical's primary entry — see
// ``BOOTSTRAP_VERTICAL_PRIMARY_ENTRY``), (2) ``POST /bootstrap/vertical-setup``
// recording the selection + install job ids in
// ``bootstrap_state.metadata``, and (3) only THEN flip the step green
// + advance the cursor. The BS.7 install-progress drawer (mounted in
// ``components/providers.tsx`` via ``<InstallProgressDrawerLive />``)
// picks up the SSE ``installer_progress`` events automatically.
//
// Three paths flip the step green:
//   * ``Confirm picks`` with ≥ 1 selection → BS.9.5 batch enqueue
//     resolves, then ``recordVerticalSetup`` succeeds, then
//     ``onCompleted()`` (any failure shows an inline error and the
//     pill stays pending so the operator can retry).
//   * ``Skip`` — pure client-only flip (no backend call). Means
//     ``backend.bootstrap._verticals_chosen()`` keeps returning False
//     and re-opening the step still shows the picker; the operator
//     can come back later via ``Settings → Platforms``.
//   * Re-opening the step after a partial commit: any failure that
//     halted before the backend record landed leaves the picker open
//     so the operator sees what they had selected.

function VerticalSetupStep({
  alreadyGreen,
  onCompleted,
}: {
  alreadyGreen: boolean
  onCompleted: () => void
}) {
  // BS.9.5 — keeps the Mobile-only AndroidApiSelector mounted alongside
  // the picker. Defaults to the canonical preset; BS.9.4 owns the
  // shape + clamping.
  const [androidApi, setAndroidApi] = useState<AndroidApiSelection>(
    DEFAULT_ANDROID_API_SELECTION,
  )
  const [selectedNow, setSelectedNow] = useState<readonly BootstrapVerticalId[]>(
    [],
  )
  const [busy, setBusy] = useState(false)
  // ``commitErr`` carries either the network-level error message or
  // the structured ``kind`` from the backend so the inline banner can
  // pick a remediation hint (currently we only echo the message).
  const [commitErr, setCommitErr] = useState<string | null>(null)
  // Surface the resolved install job ids so an operator who lost the
  // drawer (collapsed it before scrolling) can still see what was
  // queued and how many. Kept as the most-recent successful commit
  // payload — re-commit overwrites it.
  const [lastCommit, setLastCommit] = useState<{
    verticals: readonly BootstrapVerticalId[]
    jobs: InstallJob[]
  } | null>(null)

  const handleCommit = useCallback(
    async (payload: BootstrapVerticalCommitPayload) => {
      setBusy(true)
      setCommitErr(null)

      // Step 1 — enqueue one install job per selected vertical. Each
      // POST runs through the existing R20-A PEP HOLD path; the
      // operator approves once per vertical via the global
      // ToastCenter coaching card.
      const enqueued: InstallJob[] = []
      try {
        for (const v of payload.verticals_selected) {
          const entryId = BOOTSTRAP_VERTICAL_PRIMARY_ENTRY[v]
          if (!entryId) continue
          const metadata: Record<string, unknown> = {
            vertical: v,
            source: "bootstrap_wizard",
          }
          if (v === "mobile") {
            metadata.android_api = androidApi
          }
          const job = await createInstallJob(entryId, { metadata })
          enqueued.push(job)
        }
      } catch (err) {
        setBusy(false)
        const msg =
          err instanceof Error ? err.message : String(err ?? "unknown error")
        setCommitErr(`Install enqueue failed: ${msg}`)
        return
      }

      // Step 2 — record the selection + the install job ids on the
      // bootstrap_state row so the audit trail captures which jobs
      // this step kicked off.
      try {
        await bootstrapRecordVerticalSetup({
          verticals_selected: [...payload.verticals_selected],
          install_job_ids: enqueued.map((j) => j.id),
          android_api: payload.verticals_selected.includes("mobile")
            ? androidApi
            : null,
        })
      } catch (err) {
        setBusy(false)
        const msg =
          err instanceof Error ? err.message : String(err ?? "unknown error")
        setCommitErr(`Recording vertical setup failed: ${msg}`)
        return
      }

      setLastCommit({
        verticals: [...payload.verticals_selected],
        jobs: enqueued,
      })
      setBusy(false)
      onCompleted()
    },
    [androidApi, onCompleted],
  )

  const mobileSelected = selectedNow.includes("mobile")
  const enqueuedCount = lastCommit?.jobs.length ?? 0

  return (
    <div
      data-testid="bootstrap-vertical-setup-step"
      data-already-green={alreadyGreen ? "true" : "false"}
      data-busy={busy ? "true" : "false"}
      data-mobile-selected={mobileSelected ? "true" : "false"}
      className="flex flex-col gap-3 p-4 rounded border border-[var(--border)] bg-[var(--background)]"
    >
      {alreadyGreen && (
        <div
          data-testid="bootstrap-vertical-setup-complete"
          className="flex items-center gap-2 p-2 rounded border border-[var(--status-green)] bg-[var(--status-green)]/10 font-mono text-[11px] text-[var(--status-green)]"
        >
          <Check size={12} />
          Marked complete — {lastCommit
            ? `${enqueuedCount} install job${enqueuedCount === 1 ? "" : "s"} queued (see install drawer for progress)`
            : "skip applied. Verticals can be revisited later from "}
          {!lastCommit && <code>Settings → Platforms</code>}
          {!lastCommit && "."}
        </div>
      )}
      <div className="flex items-center gap-2 font-mono text-[10px] tracking-wider text-[var(--muted-foreground)]">
        <span>OPTIONAL</span>
        <code className="px-1.5 py-0.5 rounded bg-[var(--muted)]/50 text-[var(--foreground)]">
          not a finalize gate
        </code>
      </div>
      <p className="font-mono text-[11px] text-[var(--muted-foreground)] leading-relaxed">
        Pick which platform verticals you want OmniSight to provision
        now — Mobile (Android / iOS), Embedded (RK / Allwinner / Aml),
        Web, Software, Cross-toolchain. Each pick batch-enqueues an
        install job into <code>/installer/jobs</code> and progress
        surfaces in the install drawer. Skipping leaves nothing
        installed; you can come back from{" "}
        <code>Settings → Platforms</code> at any time without unlocking
        the wizard again.
      </p>

      <BootstrapVerticalStep
        disabled={busy}
        onSelectionChange={setSelectedNow}
        onCommit={handleCommit}
      />

      {mobileSelected && (
        <div
          data-testid="bootstrap-vertical-setup-android-block"
          className="flex flex-col gap-2 p-3 rounded border border-[var(--artifact-purple)]/40 bg-[var(--artifact-purple)]/5"
        >
          <div className="flex items-center gap-2 font-mono text-[10px] tracking-wider text-[var(--muted-foreground)]">
            <Layers size={12} />
            <span>BS.9.4 — ANDROID API CONFIG (Mobile sub-step)</span>
          </div>
          <AndroidApiSelector
            value={androidApi}
            disabled={busy}
            onChange={setAndroidApi}
          />
        </div>
      )}

      {commitErr && (
        <div
          data-testid="bootstrap-vertical-setup-error"
          className="flex items-start gap-2 p-2 rounded border border-[var(--status-red)] bg-[var(--status-red)]/10 font-mono text-[11px] text-[var(--status-red)]"
        >
          <AlertCircle size={12} className="mt-0.5 shrink-0" />
          <span>{commitErr}</span>
        </div>
      )}

      {busy && (
        <div
          data-testid="bootstrap-vertical-setup-busy"
          className="flex items-center gap-2 p-2 rounded border border-[var(--artifact-purple)]/50 bg-[var(--artifact-purple)]/10 font-mono text-[11px] text-[var(--foreground)]"
        >
          <Loader2 size={12} className="animate-spin" />
          <span>
            Enqueueing install jobs — see the bottom-right install
            drawer for progress.
          </span>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          data-testid="bootstrap-vertical-setup-skip"
          onClick={onCompleted}
          disabled={busy}
          className="flex items-center gap-2 px-3 py-2 rounded bg-[var(--artifact-purple)] text-white font-mono text-xs font-semibold hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          <Check size={12} />
          Skip — configure verticals later in Settings → Platforms
        </button>
      </div>

      {/* Roadmap placeholder — kept after BS.9.5 wiring so the
       *  BS.9.2 contract (placeholder enumerates BS.9.3 / 9.4 / 9.5)
       *  still reads as "what's still pending"; BS.9.6 covers the
       *  testid stays put for documentation. */}
      <div
        data-testid="bootstrap-vertical-setup-placeholder"
        className="flex flex-col gap-2 p-3 rounded border border-dashed border-[var(--border)] bg-[var(--muted)]/20 font-mono text-[10px] text-[var(--muted-foreground)] leading-relaxed"
      >
        <div className="flex items-center gap-2">
          <Layers size={12} />
          <span>BS.9 SUB-STEPS</span>
        </div>
        <ul className="list-disc pl-4 space-y-0.5">
          <li>BS.9.3 — vertical multi-pick chips (delivered above).</li>
          <li>
            BS.9.4 — Android API range selector (delivered above when
            Mobile is checked).
          </li>
          <li>
            BS.9.5 — Confirm picks → batch <code>/installer/jobs</code>{" "}
            + bootstrap_state record (delivered above).
          </li>
        </ul>
        <p>BS.9.6 deepens the regression test surface for this step.</p>
      </div>
    </div>
  )
}

// ─── L5 — Step 4 (parallel health check: 4 live ticks) ──────────────
//
// Polls ``POST /api/v1/bootstrap/parallel-health-check`` every 3s and
// renders one row per probe (backend / frontend / DB migration / CF
// tunnel). Each row's tick flips to green the moment the latest probe
// reports ``status !== "red"`` — so the operator sees the four ticks
// turn green in real time as services finish booting (rather than
// waiting for a manual refresh).
//
// The endpoint is a probe, not a gate (per backend.routers.bootstrap):
// the wizard's overall finalize gates aren't blocked by it. The step
// is satisfied locally — every row green AND ``all_green=true`` — and
// reported back to the page so the side-pill turns green.

interface HealthRow {
  id: "backend" | "frontend" | "db_migration" | "cf_tunnel"
  label: string
  hint: string
  icon: React.ComponentType<{ size?: number; className?: string }>
}

const HEALTH_ROWS: HealthRow[] = [
  {
    id: "backend",
    label: "Backend ready",
    hint: "GET /healthz returns 2xx",
    icon: Server,
  },
  {
    id: "frontend",
    label: "Frontend ready",
    hint: "Next.js root responds <500",
    icon: Globe,
  },
  {
    id: "db_migration",
    label: "DB migration up-to-date",
    hint: "Required schema invariants present",
    icon: Database,
  },
  {
    id: "cf_tunnel",
    label: "Cloudflare connector online",
    hint: "Or LAN-only skip recorded at Step 3",
    icon: Cloud,
  },
]

const SERVICE_HEALTH_POLL_MS = 3000

function HealthRowItem({
  row,
  result,
  redStreak = 0,
}: {
  row: HealthRow
  result: BootstrapHealthCheckResult | undefined
  /** Number of consecutive red probes for this row. When ``status``
   *  is ``red`` and streak < HEALTH_ROW_RED_STRIKES, the row displays
   *  green-but-verifying — a single transient failure never paints
   *  the row red. */
  redStreak?: number
}) {
  const Icon = row.icon
  // Tri-state: undefined while we have no observation yet, then driven
  // by the latest probe. ``skipped`` counts as green (LAN-only).
  const rawStatus = result?.status ?? "pending"
  // Hysteresis: if raw is red but the streak is below threshold, the
  // effective display status stays "green" (treating the failure as
  // transient until proven repeated). The verifying badge gives the
  // operator visual evidence that a re-check is pending.
  const isRawRed = rawStatus === "red"
  const verifying = isRawRed && redStreak < HEALTH_ROW_RED_STRIKES
  const status = verifying ? "green" : rawStatus
  const isGreen = status === "green" || status === "skipped"
  const isRed = status === "red"

  const glyph = isGreen ? (
    <Check size={14} className="text-[var(--status-green)]" />
  ) : isRed ? (
    <AlertCircle size={14} className="text-[var(--destructive)]" />
  ) : (
    <Loader2 size={14} className="animate-spin text-[var(--muted-foreground)]" />
  )
  const borderClass = isGreen
    ? "border-[var(--status-green)]"
    : isRed
      ? "border-[var(--destructive)]"
      : "border-[var(--border)]"

  // Latency rendered in ms; "skipped" rows show that label instead so
  // the operator can tell green-because-skipped from green-because-live.
  const latencyText =
    status === "skipped"
      ? "skipped (LAN-only)"
      : result?.latency_ms != null
        ? `${result.latency_ms}ms`
        : status === "pending"
          ? "probing…"
          : ""

  return (
    <div
      data-testid={`bootstrap-service-health-row-${row.id}`}
      data-status={status}
      data-green={isGreen ? "true" : "false"}
      className={`flex items-center gap-3 p-3 rounded border ${borderClass} bg-[var(--background)]`}
    >
      <span className="flex items-center justify-center w-7 h-7 rounded-full border border-[var(--border)] bg-[var(--card)] shrink-0">
        {glyph}
      </span>
      <Icon size={14} className="text-[var(--muted-foreground)] shrink-0" />
      <div className="flex-1 min-w-0">
        <div className="font-mono text-xs text-[var(--foreground)]">
          {row.label}
        </div>
        <div className="font-mono text-[10px] text-[var(--muted-foreground)] truncate">
          {row.hint}
        </div>
        {result?.detail && (
          <div
            className="font-mono text-[10px] text-[var(--muted-foreground)] truncate"
            title={result.detail}
          >
            {result.detail}
          </div>
        )}
      </div>
      <span
        className={`font-mono text-[10px] tracking-wider shrink-0 ${
          isGreen
            ? "text-[var(--status-green)]"
            : isRed
              ? "text-[var(--destructive)]"
              : "text-[var(--muted-foreground)]"
        }`}
      >
        {latencyText}
      </span>
      {/* Verifying badge — shown when the raw probe returned red but
          the hysteresis window is still absorbing it. Communicates
          "we saw a blip, we're checking" without flipping the row
          red in the user's face. */}
      {verifying && (
        <span
          data-testid={`bootstrap-service-health-row-${row.id}-verifying`}
          className="inline-flex items-center gap-1 rounded-sm border border-[var(--neural-blue)]/40 bg-[var(--neural-blue)]/5 px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wider text-[var(--neural-blue)] shrink-0"
          title="One transient failure detected — re-checking."
        >
          <Loader2 size={9} className="animate-spin" />
          verifying
        </span>
      )}
    </div>
  )
}

function StartServicesErrorBanner({
  kind,
  detail,
  stderrTail,
  mode,
}: {
  kind: BootstrapStartServicesKind
  detail: string
  stderrTail: string
  mode: string
}) {
  const copy = BOOTSTRAP_START_SERVICES_KIND_COPY[kind]
  return (
    <div
      role="alert"
      data-testid="bootstrap-start-services-error"
      data-kind={kind}
      data-mode={mode}
      className="flex flex-col gap-1 p-3 rounded border border-[var(--destructive)] bg-[var(--destructive)]/10"
    >
      <div className="flex items-center gap-2 font-mono text-[11px] font-semibold text-[var(--destructive)]">
        <AlertCircle size={12} /> {copy.title}
      </div>
      <p className="font-mono text-[11px] text-[var(--destructive)] break-words">
        {detail}
      </p>
      <p className="font-mono text-[10px] text-[var(--muted-foreground)] leading-relaxed">
        {copy.hint}
      </p>
      {stderrTail && (
        <pre
          data-testid="bootstrap-start-services-stderr"
          className="mt-1 p-2 rounded border border-[var(--border)] bg-[var(--muted)]/30 font-mono text-[10px] text-[var(--foreground)] whitespace-pre-wrap break-words max-h-40 overflow-auto"
        >
          {stderrTail}
        </pre>
      )}
    </div>
  )
}

// Human-readable one-liner per (status, mode) combo returned by
// /bootstrap/start-services. Centralising here so the Step-5 UI
// doesn't have to inline string conditionals + so future modes can
// be added with a single-line addition. Null = "render the generic
// launched/mode line".
function _startServicesOkCopy(
  status: string,
  mode: string,
): { label: string; tone: "green" | "info" } {
  if (status === "managed_externally") {
    return {
      label:
        "managed externally — host runs `docker compose up -d` + " +
        "`restart: always` auto-recovers. This button is a no-op here.",
      tone: "info",
    }
  }
  if (status === "already_running") {
    if (mode === "dev") {
      return {
        label: "already running — dev mode (uvicorn / next dev in-process)",
        tone: "green",
      }
    }
    return { label: `already running (${mode})`, tone: "green" }
  }
  return { label: `launched (${mode})`, tone: "green" }
}

function StartServicesPanel({
  anyRed,
  onStartResolved,
}: {
  anyRed: boolean
  onStartResolved: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [errKind, setErrKind] = useState<BootstrapStartServicesKind | null>(null)
  const [errDetail, setErrDetail] = useState<string>("")
  const [errStderr, setErrStderr] = useState<string>("")
  const [errMode, setErrMode] = useState<string>("")
  const [okResult, setOkResult] = useState<BootstrapStartServicesResponse | null>(null)

  const run = useCallback(async () => {
    setBusy(true)
    setErrKind(null)
    setErrDetail("")
    setErrStderr("")
    setErrMode("")
    setOkResult(null)
    try {
      const result = await bootstrapStartServices()
      setOkResult(result)
      // Let the parent re-probe so any row that was red flips on the
      // next health tick without waiting for the 3s interval.
      onStartResolved()
    } catch (err) {
      if (err instanceof BootstrapStartServicesError) {
        setErrKind(err.kind)
        setErrDetail(err.detail || err.message)
        setErrStderr(err.stderr_tail || "")
        setErrMode(err.mode || "")
      } else {
        const msg = err instanceof Error ? err.message : String(err)
        setErrDetail(msg)
        setErrKind("unit_failed")
      }
    } finally {
      setBusy(false)
    }
  }, [onStartResolved])

  // Description varies by whether the operator actually needs this
  // tool right now. All-green → it's a "kick the tyres" affordance;
  // a row stuck red → it's the primary CTA to get past the step.
  const description = anyRed
    ? (
      <>
        A probe above is stuck red.{" "}
        <strong className="text-[var(--foreground)]">
          Try the launcher first
        </strong>{" "}
        — the backend auto-detects deploy mode (systemd /
        docker-compose / dev). If the launcher can't act from here
        (e.g. compose-managed stack), the banner explains where the
        real control plane lives so you can fix it on the host.
      </>
    )
    : (
      <>
        Everything's green. The launcher is idle here — press it only
        if you want to force a re-kick of the services. In a
        compose-managed deployment this button is a no-op (services
        come up via the host's <code>docker compose up -d</code> and
        <code className="ml-1">restart: always</code> recovers crashes).
      </>
    )

  return (
    <div
      data-testid="bootstrap-start-services-panel"
      data-any-red={anyRed ? "true" : "false"}
      className="flex flex-col gap-2 p-3 rounded border border-dashed border-[var(--border)] bg-[var(--background)]"
    >
      <div className="flex items-center gap-2 font-mono text-[10px] tracking-wider text-[var(--muted-foreground)]">
        <span>LAUNCHER</span>
        <code className="px-1.5 py-0.5 rounded bg-[var(--muted)]/50 text-[var(--foreground)]">
          POST /bootstrap/start-services
        </code>
        {!anyRed && (
          <span
            data-testid="bootstrap-start-services-optional"
            className="rounded border border-[var(--border)] px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wider text-[var(--muted-foreground)]"
          >
            optional
          </span>
        )}
      </div>
      <p className="font-mono text-[11px] text-[var(--muted-foreground)] leading-relaxed">
        {description}
      </p>
      <div className="flex items-center gap-2 flex-wrap">
        <button
          type="button"
          data-testid="bootstrap-start-services-button"
          onClick={() => void run()}
          disabled={busy}
          className={`flex items-center gap-1 font-mono text-[11px] px-2 py-1 rounded border transition-colors disabled:opacity-40 ${
            anyRed
              ? "border-[var(--artifact-purple)] bg-[var(--artifact-purple)] text-white font-semibold hover:opacity-90"
              : "border-[var(--border)] text-[var(--muted-foreground)] hover:bg-[var(--muted)]/40"
          }`}
        >
          {busy ? (
            <Loader2 size={12} className="animate-spin" />
          ) : (
            <Rocket size={12} />
          )}
          {anyRed ? "Launch services" : "Relaunch (idle)"}
        </button>
        {okResult && (() => {
          const copy = _startServicesOkCopy(okResult.status, okResult.mode)
          const toneClass =
            copy.tone === "green"
              ? "text-[var(--status-green)]"
              : "text-[var(--neural-blue)]"
          const Glyph = copy.tone === "green" ? Check : Activity
          return (
            <span
              data-testid="bootstrap-start-services-ok"
              data-status={okResult.status}
              data-mode={okResult.mode}
              className={`inline-flex items-center gap-1 font-mono text-[11px] ${toneClass}`}
            >
              <Glyph size={12} />
              {copy.label}
            </span>
          )
        })()}
      </div>
      {errKind && (
        <StartServicesErrorBanner
          kind={errKind}
          detail={errDetail}
          stderrTail={errStderr}
          mode={errMode}
        />
      )}
    </div>
  )
}

// Hysteresis threshold — how many consecutive red probes a row must
// accumulate before the UI actually flips it red. 2 means a single
// transient red (CF edge hiccup, caddy LB temporarily landing on a
// re-starting replica, etc.) is masked; the row stays visually green
// but carries a subtle "verifying" badge so the operator knows a
// re-check is in flight.
const HEALTH_ROW_RED_STRIKES = 2
// How many consecutive transport-level XHR failures are allowed
// before the sidebar pill gets nudged red. Below this, we keep the
// pill on its last known state — a blip should not roll the wizard
// cursor backward.
const HEALTH_XHR_ERROR_STRIKES = 3

function ServiceHealthStep({
  onChanged,
}: {
  onChanged: (allGreen: boolean) => void
}) {
  const [snapshot, setSnapshot] = useState<BootstrapParallelHealthCheckResponse | null>(
    null,
  )
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Tracks how many probes have come back at least once, so the UI can
  // show "probing…" on first paint instead of stale "all red".
  const [probeCount, setProbeCount] = useState(0)
  // Per-row consecutive-red counter. Refs rather than state because
  // updating them must NOT trigger a re-render on its own — the
  // re-render is driven by setSnapshot below, and we read the final
  // counter value during render via memo.
  const redStreakRef = useRef<Record<string, number>>({})
  const xhrErrorStreakRef = useRef<number>(0)
  // Mirror ref → state only once per probe so the memo that renders
  // the rows actually recomputes. Keeping the visible snapshot of
  // the streak lets HealthRowItem show the "verifying" hint too.
  const [redStreak, setRedStreak] = useState<Record<string, number>>({})
  const [xhrErrorStreak, setXhrErrorStreak] = useState<number>(0)

  // Stable ref over ``onChanged`` so the polling effect doesn't get
  // torn down + restarted every parent re-render (which would consume
  // the next mocked response in tests and double-invoke the probe in
  // production). The latest callback is always called via the ref.
  const onChangedRef = useRef(onChanged)
  useEffect(() => {
    onChangedRef.current = onChanged
  }, [onChanged])

  const runProbe = useCallback(async () => {
    setBusy(true)
    setError(null)
    try {
      const next = await bootstrapParallelHealthCheck()
      // Update per-row red-streak BEFORE deciding onChanged — that
      // way a single red doesn't flip the sidebar pill to "missing".
      const streak = { ...redStreakRef.current }
      for (const row of HEALTH_ROWS) {
        const s = next[row.id]?.status
        if (s === "green" || s === "skipped") {
          streak[row.id] = 0
        } else if (s === "red") {
          streak[row.id] = (streak[row.id] ?? 0) + 1
        }
      }
      redStreakRef.current = streak
      xhrErrorStreakRef.current = 0
      setSnapshot(next)
      setRedStreak(streak)
      setXhrErrorStreak(0)
      setProbeCount((n) => n + 1)
      // onChanged reflects the *stabilized* view — a row only counts
      // as red if its streak has crossed the threshold. This keeps
      // the sidebar pill from flickering on single-shot failures.
      const stableAllGreen = HEALTH_ROWS.every((row) => {
        const s = next[row.id]?.status
        if (s === "green" || s === "skipped") return true
        // status=red: only count as red after N strikes
        return (streak[row.id] ?? 0) < HEALTH_ROW_RED_STRIKES
      })
      onChangedRef.current(stableAllGreen)
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      setError(msg)
      // Transport error = we have no new info; don't flip rows and
      // don't nudge the sidebar pill until the error streak crosses
      // threshold (signalling a real outage, not a CF jitter).
      xhrErrorStreakRef.current += 1
      setXhrErrorStreak(xhrErrorStreakRef.current)
      if (xhrErrorStreakRef.current >= HEALTH_XHR_ERROR_STRIKES) {
        onChangedRef.current(false)
      }
    } finally {
      setBusy(false)
    }
  }, [])

  // Probe immediately + every 3s. We keep polling even after all-green
  // so a service crash flips a tick back to red without a manual
  // refresh. The interval is cheap — single POST that fans out four
  // checks server-side.
  useEffect(() => {
    void runProbe()
    const handle = setInterval(() => {
      void runProbe()
    }, SERVICE_HEALTH_POLL_MS)
    return () => clearInterval(handle)
  }, [runProbe])

  const greenCount = snapshot
    ? HEALTH_ROWS.reduce((acc, row) => {
        const r = snapshot[row.id]
        return acc + (r && (r.status === "green" || r.status === "skipped") ? 1 : 0)
      }, 0)
    : 0
  const allGreen = snapshot?.all_green === true

  return (
    <div
      data-testid="bootstrap-service-health-step"
      data-all-green={allGreen ? "true" : "false"}
      data-green-count={greenCount}
      data-probe-count={probeCount}
      className="flex flex-col gap-3 p-4 rounded border border-[var(--border)] bg-[var(--background)]"
    >
      <div className="flex items-center gap-2 font-mono text-[10px] tracking-wider text-[var(--muted-foreground)]">
        <span>PROBE</span>
        <code className="px-1.5 py-0.5 rounded bg-[var(--muted)]/50 text-[var(--foreground)]">
          POST /bootstrap/parallel-health-check
        </code>
      </div>
      <p className="font-mono text-[11px] text-[var(--muted-foreground)] leading-relaxed">
        Live readiness of the four bootstrap services. Each tick turns
        green the moment the matching probe comes back from the server —
        re-polled every {SERVICE_HEALTH_POLL_MS / 1000}s so a service
        crash flips its row back to red without a manual refresh.
      </p>

      <div
        data-testid="bootstrap-service-health-rows"
        className="flex flex-col gap-2"
      >
        {HEALTH_ROWS.map((row) => (
          <HealthRowItem
            key={row.id}
            row={row}
            result={snapshot ? snapshot[row.id] : undefined}
            redStreak={redStreak[row.id] ?? 0}
          />
        ))}
      </div>

      {/* Link-quality telemetry — surfaces transient XHR failures to
          the backend (CF edge hiccup, browser fetch abort, etc.)
          without flipping any row red. Hidden when the line is
          clean. Rendered as a muted hint rather than a hard error
          because a couple of blips are routine on a mobile uplink. */}
      {xhrErrorStreak > 0 && (
        <div
          data-testid="bootstrap-service-health-xhr-wobble"
          className={`flex items-center gap-2 rounded px-2 py-1 font-mono text-[10px] ${
            xhrErrorStreak >= HEALTH_XHR_ERROR_STRIKES
              ? "border border-[var(--destructive)]/40 bg-[var(--destructive)]/5 text-[var(--destructive)]"
              : "border border-[var(--neural-blue)]/30 bg-[var(--neural-blue)]/5 text-[var(--neural-blue)]"
          }`}
        >
          <Loader2 size={10} className="animate-spin" />
          <span>
            link wobble: last probe failed ({xhrErrorStreak}/
            {HEALTH_XHR_ERROR_STRIKES} before degraded) — keeping last
            known state visible while we retry.
          </span>
        </div>
      )}

      <StartServicesPanel
        anyRed={
          // Apply the same hysteresis to the "start services" CTA:
          // a row that's on a red streak below threshold is still
          // considered "verifying" here, not "broken". Prevents the
          // big purple "Start services" button from flashing in on
          // one bad probe.
          snapshot != null &&
          HEALTH_ROWS.some(
            (row) =>
              snapshot[row.id]?.status === "red" &&
              (redStreak[row.id] ?? 0) >= HEALTH_ROW_RED_STRIKES,
          )
        }
        onStartResolved={() => void runProbe()}
      />

      <div className="flex items-center justify-between">
        <span
          data-testid="bootstrap-service-health-summary"
          className={`font-mono text-[11px] ${
            allGreen
              ? "text-[var(--status-green)]"
              : "text-[var(--muted-foreground)]"
          }`}
        >
          {allGreen ? (
            <span className="flex items-center gap-1">
              <Check size={12} /> {greenCount}/4 services green · all systems
              ready
            </span>
          ) : (
            <span>
              {greenCount}/4 services green
              {snapshot ? ` · ${snapshot.elapsed_ms}ms last probe` : ""}
            </span>
          )}
        </span>
        <button
          type="button"
          data-testid="bootstrap-service-health-recheck"
          onClick={() => void runProbe()}
          disabled={busy}
          className="flex items-center gap-1 font-mono text-[11px] px-2 py-1 rounded border border-[var(--border)] hover:bg-[var(--muted)]/40 disabled:opacity-40"
        >
          {busy ? (
            <Loader2 size={12} className="animate-spin" />
          ) : (
            <RefreshCw size={12} />
          )}
          Re-check
        </button>
      </div>

      {error && (
        <p
          role="alert"
          data-testid="bootstrap-service-health-error"
          className="font-mono text-[11px] text-[var(--destructive)] break-words"
        >
          {error}
        </p>
      )}
    </div>
  )
}

// ─── L6 — Step 5 (smoke test subset: compile-flash + cross-compile DAGs) ──

/**
 * Steps the smoke pane offers as jump-back targets when the run fails.
 * Order matches the wizard sidebar; ``services_ready`` sits last because
 * it's the most common culprit for a host_native compile-flash failure.
 */
const SMOKE_JUMP_BACK_STEPS: Array<{
  id: StepId
  label: string
  hint: string
}> = [
  {
    id: "admin_password",
    label: "Step 1 — Admin Password",
    hint: "Audit chain failures usually trace back to a tenant/admin row",
  },
  {
    id: "llm_provider",
    label: "Step 2 — LLM Provider",
    hint: "Re-validate provider key if planner / validator errors mention LLM",
  },
  {
    id: "cf_tunnel",
    label: "Step 3 — Cloudflare Tunnel",
    hint: "Re-provision (or skip) the tunnel if connector probes failed",
  },
  {
    id: "services_ready",
    label: "Step 4 — Service Health",
    hint: "Re-run the parallel-health-check probes before retrying smoke",
  },
]

/**
 * Heuristic: pick the most likely culprit step based on the smoke
 * failure shape. Returns ``null`` when nothing obvious stands out so
 * the operator picks freely from the four jump-back buttons.
 */
function _diagnoseSmokeFailure(
  result: BootstrapSmokeSubsetResponse | null,
  networkError: string | null,
): StepId | null {
  if (networkError) return "services_ready"
  if (!result) return null
  if (!result.audit_chain.ok) return "admin_password"
  for (const run of result.runs) {
    if (run.ok) continue
    for (const err of run.validation_errors) {
      const blob = `${err.rule} ${err.message}`.toLowerCase()
      if (blob.includes("llm") || blob.includes("provider")) {
        return "llm_provider"
      }
      if (blob.includes("tunnel") || blob.includes("cloudflare")) {
        return "cf_tunnel"
      }
      if (
        blob.includes("platform") ||
        blob.includes("compile") ||
        blob.includes("target") ||
        blob.includes("runner") ||
        blob.includes("ready")
      ) {
        return "services_ready"
      }
    }
  }
  return "services_ready"
}

function SmokeSubsetStep({
  alreadyGreen,
  onPassed,
  onJumpToStep,
  finalize,
}: {
  alreadyGreen: boolean
  onPassed: () => Promise<unknown>
  onJumpToStep?: (id: StepId) => void
  finalize?: {
    allGatesGreen: boolean
    busy: boolean
    missingSteps: string[]
    onFinalize: () => void
  }
}) {
  const [result, setResult] = useState<BootstrapSmokeSubsetResponse | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Round 3-D: COMBAT DEPLOY gate. When cinema mode is ON the first
  // click on "Run smoke subset" arms the sequence (shows an ARMED
  // confirm overlay listing the DAGs + audit-chain checks); a second
  // click on PROCEED actually fires. Cinema OFF → the old direct
  // path. Keeps operator consent explicit and adds theatrical weight
  // to the step without changing the API contract.
  const cinema = useCinemaMode()
  const [armed, setArmed] = useState(false)

  const run = useCallback(async () => {
    setArmed(false)
    setBusy(true)
    setError(null)
    try {
      // Wizard asks for "both" so the Step-5 pane can render a run
      // summary per DAG shipped in ``scripts/prod_smoke_test.py``. The
      // backend still validates+persists only (no cross-compile), so
      // including DAG #2 does not blow the wizard's fast path.
      const next = await bootstrapSmokeSubset("both")
      setResult(next)
      if (next.smoke_passed) {
        await onPassed()
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      setError(msg)
    } finally {
      setBusy(false)
    }
  }, [onPassed])

  const onRunClick = useCallback(() => {
    if (cinema.hydrated && cinema.enabled) {
      setArmed(true)
      return
    }
    void run()
  }, [cinema.hydrated, cinema.enabled, run])

  const runs = result?.runs ?? []
  const auditOk = result?.audit_chain?.ok === true
  const passed = result?.smoke_passed === true
  const smokeGreen = alreadyGreen || passed
  // Failure surface: either the call itself errored (no result) or the
  // backend returned a not-passed result. ``alreadyGreen`` suppresses
  // the jump-back panel because the gate is independently green.
  const hasFailure =
    !smokeGreen && (error !== null || (result !== null && !passed))
  const culpritStepId = _diagnoseSmokeFailure(result, error)

  return (
    <div
      data-testid="bootstrap-smoke-subset-step"
      data-smoke-passed={alreadyGreen || passed ? "true" : "false"}
      className="flex flex-col gap-3 p-4 rounded border border-[var(--border)] bg-[var(--background)]"
    >
      <div className="flex items-center gap-2 font-mono text-[10px] tracking-wider text-[var(--muted-foreground)]">
        <span>PROBE</span>
        <code className="px-1.5 py-0.5 rounded bg-[var(--muted)]/50 text-[var(--foreground)]">
          POST /bootstrap/smoke-subset
        </code>
      </div>
      <p className="font-mono text-[11px] text-[var(--muted-foreground)] leading-relaxed">
        Runs both DAGs from{" "}
        <code>scripts/prod_smoke_test.py</code> —{" "}
        <code>compile-flash host_native</code> and{" "}
        <code>cross-compile aarch64</code> — and verifies the audit-log
        hash chain. On all green the fifth gate flips and finalize
        unlocks; on red the per-DAG run summary + first-bad audit id are
        surfaced so the operator can jump back to the offending step.
      </p>

      {alreadyGreen && !result && (
        <p
          data-testid="bootstrap-smoke-already-green"
          className="font-mono text-[11px] text-[var(--status-green)]"
        >
          <Check size={12} className="inline mr-1" /> Smoke already passed —
          backend gate is green. Re-run any time to re-verify.
        </p>
      )}

      <div className="flex items-center gap-2">
        <button
          type="button"
          data-testid="bootstrap-smoke-run-button"
          onClick={onRunClick}
          disabled={busy}
          className="flex items-center gap-1 px-3 py-1.5 rounded bg-[var(--artifact-purple)] text-white font-mono text-xs font-semibold hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {busy ? (
            <Loader2 size={12} className="animate-spin" />
          ) : (
            <FlaskConical size={12} />
          )}
          {result ? "Re-run smoke" : "Run smoke subset"}
        </button>
        {result && (
          <span
            data-testid="bootstrap-smoke-elapsed"
            className="font-mono text-[10px] text-[var(--muted-foreground)]"
          >
            last run: {result.elapsed_ms}ms · subset={result.subset} ·{" "}
            {runs.length} DAG{runs.length === 1 ? "" : "s"}
          </span>
        )}
      </div>

      {result && (
        <div
          data-testid="bootstrap-smoke-result"
          data-passed={passed ? "true" : "false"}
          data-run-count={runs.length}
          className="flex flex-col gap-2 p-3 rounded border border-[var(--border)] bg-[var(--muted)]/20"
        >
          <div
            className={`flex items-center gap-2 font-mono text-xs font-semibold ${
              passed
                ? "text-[var(--status-green)]"
                : "text-[var(--destructive)]"
            }`}
          >
            {passed ? (
              <>
                <Check size={14} /> Smoke PASSED — smoke_passed gate is now
                green.
              </>
            ) : (
              <>
                <AlertCircle size={14} /> Smoke FAILED — fix the highlighted
                row and re-run.
              </>
            )}
          </div>

          {runs.length > 0 && (
            <div
              data-testid="bootstrap-smoke-runs"
              className="flex flex-col gap-2"
            >
              {runs.map((run, idx) => (
                <div
                  key={run.dag_id || `run-${idx}`}
                  data-testid="bootstrap-smoke-run-summary"
                  data-run-key={run.key || `run-${idx}`}
                  data-run-ok={run.ok ? "true" : "false"}
                  className={`flex flex-col gap-1 p-2 rounded border font-mono text-[11px] ${
                    run.ok
                      ? "border-[var(--status-green)]/30 bg-[var(--status-green)]/5"
                      : "border-[var(--destructive)]/40 bg-[var(--destructive)]/5"
                  }`}
                >
                  <div className="flex items-center gap-2 font-semibold">
                    {run.ok ? (
                      <Check
                        size={12}
                        className="text-[var(--status-green)]"
                      />
                    ) : (
                      <AlertCircle
                        size={12}
                        className="text-[var(--destructive)]"
                      />
                    )}
                    <span>{run.label}</span>
                  </div>
                  <div>
                    <span className="text-[var(--muted-foreground)]">run:</span>{" "}
                    <code>{run.run_id ?? "—"}</code>
                    {" · "}
                    <span className="text-[var(--muted-foreground)]">plan:</span>{" "}
                    <code>{run.plan_id ?? "—"}</code>
                    {" · "}
                    <span className="text-[var(--muted-foreground)]">
                      status:
                    </span>{" "}
                    <code>{run.plan_status ?? "—"}</code>
                  </div>
                  <div>
                    <span className="text-[var(--muted-foreground)]">
                      target:
                    </span>{" "}
                    <code>{run.target_platform ?? "—"}</code>
                    {" · "}
                    <span className="text-[var(--muted-foreground)]">t3:</span>{" "}
                    <code>{run.t3_runner ?? "—"}</code>
                    {" · "}
                    <span className="text-[var(--muted-foreground)]">
                      tasks:
                    </span>{" "}
                    {run.task_count}
                  </div>
                  {run.validation_errors.length > 0 && (
                    <ul
                      data-testid="bootstrap-smoke-errors"
                      className="mt-1 list-disc pl-4 text-[var(--destructive)]"
                    >
                      {run.validation_errors.map((e, i) => (
                        <li key={i}>
                          <code>{e.rule}</code>
                          {e.task_id ? ` (${e.task_id})` : ""}: {e.message}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              ))}
            </div>
          )}

          <div
            data-testid="bootstrap-smoke-audit-summary"
            data-audit-ok={auditOk ? "true" : "false"}
            data-tenant-count={result.audit_chain.tenant_count}
            className={`flex flex-col gap-0.5 p-2 rounded border font-mono text-[11px] ${
              auditOk
                ? "border-[var(--status-green)]/30 bg-[var(--status-green)]/5 text-[var(--status-green)]"
                : "border-[var(--destructive)]/40 bg-[var(--destructive)]/5 text-[var(--destructive)]"
            }`}
          >
            <div className="flex items-center gap-2 font-semibold">
              {auditOk ? <Check size={12} /> : <AlertCircle size={12} />}
              <span>
                audit_log hash chain: {auditOk ? "PASS" : "FAIL"} ·{" "}
                {result.audit_chain.tenant_count} tenant
                {result.audit_chain.tenant_count === 1 ? "" : "s"}
              </span>
            </div>
            <div className="pl-5 text-[var(--muted-foreground)]">
              {result.audit_chain.detail || "—"}
              {result.audit_chain.first_bad_id != null
                ? ` · first_bad_id=${result.audit_chain.first_bad_id}`
                : ""}
              {result.audit_chain.bad_tenants.length > 0
                ? ` · bad_tenants=${result.audit_chain.bad_tenants.join(",")}`
                : ""}
            </div>
          </div>
        </div>
      )}

      {error && (
        <p
          role="alert"
          data-testid="bootstrap-smoke-error"
          className="font-mono text-[11px] text-[var(--destructive)] break-words"
        >
          {error}
        </p>
      )}

      {hasFailure && onJumpToStep && (
        <div
          data-testid="bootstrap-smoke-jump-back"
          data-culprit={culpritStepId ?? ""}
          className="flex flex-col gap-2 p-3 rounded border border-[var(--destructive)]/40 bg-[var(--destructive)]/5"
        >
          <div className="flex items-center gap-2 font-mono text-[11px] font-semibold text-[var(--destructive)]">
            <AlertCircle size={12} />
            <span>Jump back to fix a previous step</span>
          </div>
          <p className="font-mono text-[11px] text-[var(--muted-foreground)]">
            Re-open the gate that owns the failure, fix it there, then
            return to Step 5 and re-run the smoke subset.
          </p>
          <div
            data-testid="bootstrap-smoke-jump-back-buttons"
            className="flex flex-wrap gap-2"
          >
            {SMOKE_JUMP_BACK_STEPS.map((s) => {
              const isCulprit = culpritStepId === s.id
              return (
                <button
                  key={s.id}
                  type="button"
                  data-testid={`bootstrap-smoke-jump-back-${s.id}`}
                  data-culprit={isCulprit ? "true" : "false"}
                  onClick={() => onJumpToStep(s.id)}
                  title={s.hint}
                  className={`flex items-center gap-1 px-2.5 py-1.5 rounded border font-mono text-[11px] transition ${
                    isCulprit
                      ? "border-[var(--destructive)] bg-[var(--destructive)]/15 text-[var(--destructive)] font-semibold"
                      : "border-[var(--border)] bg-[var(--background)] text-[var(--foreground)] hover:bg-[var(--muted)]/40"
                  }`}
                >
                  <ChevronLeft size={11} />
                  <span>{s.label}</span>
                  {isCulprit && (
                    <span className="ml-1 px-1 rounded bg-[var(--destructive)]/20 text-[9px] uppercase tracking-wider">
                      likely
                    </span>
                  )}
                </button>
              )
            })}
          </div>
        </div>
      )}

      {finalize && smokeGreen && (
        <div
          data-testid="bootstrap-smoke-finalize-cta"
          data-ready={finalize.allGatesGreen ? "true" : "false"}
          className="flex flex-col gap-2 p-3 rounded border border-[var(--artifact-purple)]/40 bg-[var(--artifact-purple)]/5"
        >
          <p className="font-mono text-[11px] text-[var(--foreground)]">
            All four gates green — closing the wizard writes{" "}
            <code>bootstrap_finalized=true</code> and opens the dashboard.
          </p>
          {!finalize.allGatesGreen && finalize.missingSteps.length > 0 && (
            <p className="font-mono text-[11px] text-[var(--destructive)]">
              Missing steps:{" "}
              <code>{finalize.missingSteps.join(", ")}</code>
            </p>
          )}
          <button
            type="button"
            data-testid="bootstrap-smoke-finalize-button"
            onClick={finalize.onFinalize}
            disabled={!finalize.allGatesGreen || finalize.busy}
            className="self-start flex items-center gap-2 px-3 py-2 rounded bg-[var(--artifact-purple)] text-white font-mono text-xs font-semibold hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {finalize.busy ? (
              <Loader2 size={12} className="animate-spin" />
            ) : (
              <Rocket size={12} />
            )}
            Finalize &amp; go to dashboard
          </button>
        </div>
      )}

      {/* Round 3-D: COMBAT DEPLOY confirm. Cinema-mode only. Listing
          the three artefacts the operator is about to engage so the
          "press a button" moment feels consequential. */}
      {armed && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label="smoke sequence armed — confirm to proceed"
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-md"
        >
          <div className="relative w-full max-w-lg rounded-lg border-2 border-[var(--artifact-purple)] bg-black/90 p-6 shadow-[0_0_40px_rgba(192,132,252,0.4)]">
            <div className="mb-3 flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.3em] text-[var(--artifact-purple)]">
              <Radio size={12} className="animate-pulse" />
              INITIATING SMOKE DIAGNOSTIC
            </div>
            <div
              className="mb-5 text-xl font-bold uppercase tracking-[0.15em] text-[var(--foreground)]"
              style={{ fontFamily: "var(--font-orbitron), sans-serif" }}
            >
              <span className="text-[var(--artifact-purple)]">⚠</span>{" "}
              Arm sequence confirmed?
            </div>
            <ul className="mb-5 flex flex-col gap-2 font-mono text-[11px]">
              <li className="flex items-center justify-between rounded border border-[var(--artifact-purple)]/40 bg-[var(--artifact-purple)]/5 px-3 py-2">
                <span className="text-[var(--foreground)]">
                  DAG #1 · compile-flash (host_native)
                </span>
                <span className="font-semibold uppercase tracking-wider text-[var(--artifact-purple)]">
                  [ARMED]
                </span>
              </li>
              <li className="flex items-center justify-between rounded border border-[var(--artifact-purple)]/40 bg-[var(--artifact-purple)]/5 px-3 py-2">
                <span className="text-[var(--foreground)]">
                  DAG #2 · cross-compile (aarch64)
                </span>
                <span className="font-semibold uppercase tracking-wider text-[var(--artifact-purple)]">
                  [ARMED]
                </span>
              </li>
              <li className="flex items-center justify-between rounded border border-[var(--artifact-purple)]/40 bg-[var(--artifact-purple)]/5 px-3 py-2">
                <span className="text-[var(--foreground)]">
                  Audit chain · Merkle verify
                </span>
                <span className="font-semibold uppercase tracking-wider text-[var(--artifact-purple)]">
                  [ARMED]
                </span>
              </li>
            </ul>
            <p className="mb-5 font-mono text-[10px] leading-relaxed text-[var(--muted-foreground)]">
              Estimated runtime ~2 minutes. Safe to abort — nothing
              persists until both DAGs validate + submit AND the audit
              chain re-verifies.
            </p>
            <div className="flex items-center justify-end gap-2">
              <button
                type="button"
                onClick={() => setArmed(false)}
                data-testid="bootstrap-smoke-abort"
                className="rounded border border-[var(--border)] bg-transparent px-4 py-2 font-mono text-xs font-semibold uppercase tracking-wider text-[var(--muted-foreground)] hover:border-[var(--foreground)] hover:text-[var(--foreground)]"
              >
                ABORT
              </button>
              <button
                type="button"
                onClick={() => void run()}
                data-testid="bootstrap-smoke-proceed"
                className="flex items-center gap-1.5 rounded border border-[var(--artifact-purple)] bg-[var(--artifact-purple)] px-4 py-2 font-mono text-xs font-bold uppercase tracking-wider text-white hover:opacity-90 shadow-[0_0_20px_rgba(192,132,252,0.5)]"
              >
                <FlaskConical size={12} />
                PROCEED
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function _shortCommit(value: string): string {
  const trimmed = value.trim()
  if (!trimmed) return "unknown"
  return trimmed.length > 12 ? trimmed.slice(0, 12) : trimmed
}

function FrontendFreshnessStep({
  freshness,
}: {
  freshness: BootstrapFrontendFreshness
}) {
  const isStale = freshness.status === "stale"
  const isUnknown = freshness.status === "unknown"
  const border = isStale
    ? "border-[var(--status-red)] bg-[var(--status-red)]/10"
    : isUnknown
      ? "border-[var(--status-yellow,#d97706)] bg-[var(--status-yellow,#d97706)]/10"
      : "border-[var(--status-green)] bg-[var(--status-green)]/10"
  const text = isStale
    ? "text-[var(--status-red)]"
    : isUnknown
      ? "text-[var(--status-yellow,#d97706)]"
      : "text-[var(--status-green)]"

  return (
    <div
      data-testid="bootstrap-frontend-freshness"
      data-status={freshness.status}
      className={`flex flex-col gap-3 p-4 rounded border ${border}`}
    >
      <div className={`flex items-center gap-2 font-mono text-xs ${text}`}>
        {isStale ? <AlertCircle size={14} /> : <Check size={14} />}
        <span>{freshness.detail}</span>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
        <div className="p-3 rounded border border-[var(--border)] bg-[var(--background)]">
          <div className="font-mono text-[10px] text-[var(--muted-foreground)]">
            Prod build
          </div>
          <code
            data-testid="bootstrap-frontend-prod-commit"
            className="font-mono text-xs text-[var(--foreground)] break-all"
          >
            {_shortCommit(freshness.prod_build_commit)}
          </code>
        </div>
        <div className="p-3 rounded border border-[var(--border)] bg-[var(--background)]">
          <div className="font-mono text-[10px] text-[var(--muted-foreground)]">
            Master HEAD
          </div>
          <code
            data-testid="bootstrap-frontend-master-commit"
            className="font-mono text-xs text-[var(--foreground)] break-all"
          >
            {_shortCommit(freshness.master_head_commit)}
          </code>
        </div>
        <div className="p-3 rounded border border-[var(--border)] bg-[var(--background)]">
          <div className="font-mono text-[10px] text-[var(--muted-foreground)]">
            Lag
          </div>
          <code
            data-testid="bootstrap-frontend-lag"
            className={`font-mono text-xs ${text}`}
          >
            {freshness.lag_commits} commits
          </code>
        </div>
      </div>
      <p className="font-mono text-[10px] text-[var(--muted-foreground)] leading-relaxed">
        Alert threshold: Prometheus fires when
        <code> omnisight_frontend_build_lag_commits </code>
        reaches 10. CI fails earlier when more than 5 frontend commits
        land after the recorded frontend deploy.
      </p>
    </div>
  )
}

function StepBodyPlaceholder({ step }: { step: StepDef }) {
  // Each step's actual UI lands in its own TODO slot (L3–L5). Until then
  // the shell just surfaces what this step IS so the operator knows what's
  // coming + how it maps to the backend gate.
  const map: Record<StepId, { gate: string; todo: string }> = {
    admin_password: {
      gate: "admin_password_default === false",
      todo: "L2 — force change of `omnisight-admin` + password strength check",
    },
    init_tenant: {
      gate: "optional (not a finalize gate)",
      todo: "Y7 #283 — POST /bootstrap/init-tenant + super-admin seeding",
    },
    llm_provider: {
      gate: "llm_provider_configured === true",
      todo: "L3 — provider picker + live `provider.ping()` with key validation",
    },
    cf_tunnel: {
      gate: "cf_tunnel_configured === true",
      todo: "L4 — create/link Cloudflare tunnel or explicit skip for LAN-only",
    },
    git_forge: {
      gate: "optional (not a finalize gate)",
      todo: "B14 Part A — GitHub / GitLab / Gerrit per-tab setup + Test Connection",
    },
    vertical_setup: {
      gate: "optional (not a finalize gate)",
      todo: "BS.9.3+ — vertical multi-pick + Android API selector + batch enqueue",
    },
    services_ready: {
      gate: "parallel-health-check.all_green === true",
      todo: "L5 — backend/frontend/DB/CF connector live ticks",
    },
    frontend_freshness: {
      gate: "omnisight_frontend_build_lag_commits < 10",
      todo: "BP.W3.14 — prod frontend build commit vs master HEAD",
    },
    smoke: {
      gate: "smoke_passed === true",
      todo: "L5 — end-to-end smoke runner + result pane",
    },
    finalize: {
      gate: "all four gates green",
      todo: "Confirm and call POST /api/v1/bootstrap/finalize",
    },
  }
  const entry = map[step.id]
  return (
    <div className="flex flex-col gap-3 p-4 rounded border border-dashed border-[var(--border)] bg-[var(--background)]">
      <div className="flex items-center gap-2 font-mono text-[10px] tracking-wider text-[var(--muted-foreground)]">
        <span>GATE</span>
        <code className="px-1.5 py-0.5 rounded bg-[var(--muted)]/50 text-[var(--foreground)]">
          {entry.gate}
        </code>
      </div>
      <p className="font-mono text-xs text-[var(--muted-foreground)] leading-relaxed">
        {entry.todo}
      </p>
      <p className="font-mono text-[10px] text-[var(--muted-foreground)] italic">
        Wizard shell only — this step's controls land in a follow-up TODO.
      </p>
    </div>
  )
}

// ─── Page component ─────────────────────────────────────────────────

function _emptyLocalGreen(): Record<StepId, boolean> {
  // Centralised so STEPS additions don't drift from the seed value.
  // Every step starts red — the "Skip" affordance on optional steps
  // (git_forge) + the probe result on local-only steps
  // (services_ready) flip them to green. An earlier version seeded
  // git_forge=true to keep the auto-advance cursor from stalling on
  // it, but that had the awful side-effect of rendering the
  // "Marked complete — skip applied" banner BEFORE the operator
  // touched the step, AND making the Skip button a no-op (the
  // setState bailed out because prev === next value). See the
  // GitForgeStep's onCompleted callback on the page component for
  // the replacement behaviour: explicit cursor advance on Skip/Save.
  const out = {} as Record<StepId, boolean>
  for (const s of STEPS) out[s.id] = false
  return out
}

export default function BootstrapPage() {
  const router = useRouter()
  const [status, setStatus] = useState<BootstrapStatusResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [activeId, setActiveId] = useState<StepId>("admin_password")
  const [finalizing, setFinalizing] = useState(false)
  // Locally-tracked greens for steps the backend doesn't gate (currently
  // only ``services_ready`` — the parallel-health-check is a probe, not
  // a finalize gate).
  const [localGreen, setLocalGreen] = useState<Record<StepId, boolean>>(
    () => _emptyLocalGreen(),
  )

  const setLocalGreenFor = useCallback((id: StepId, value: boolean) => {
    setLocalGreen((prev) =>
      prev[id] === value ? prev : { ...prev, [id]: value },
    )
  }, [])

  const reloadStatus = useCallback(async () => {
    try {
      const next = await getBootstrapStatus()
      setStatus(next)
      setError(null)
      if (next.finalized) {
        // Backend already flipped the flag — get out of the wizard.
        router.replace("/")
      }
      return next
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      setError(msg)
      return null
    } finally {
      setLoading(false)
    }
  }, [router])

  useEffect(() => {
    void reloadStatus()
  }, [reloadStatus])

  // Auto-advance cursor to the first un-green step when status changes —
  // but never yank a step the operator explicitly clicked on.
  const [userPinned, setUserPinned] = useState(false)
  useEffect(() => {
    if (!status || userPinned) return
    const firstRed = STEPS.find(
      (s) => !s.isGreen(status.status, status.finalized, localGreen, status),
    )
    if (firstRed) setActiveId(firstRed.id)
  }, [status, userPinned, localGreen])

  const stepStates = useMemo(() => {
    if (!status) return {} as Record<StepId, "green" | "pending" | "active">
    const out: Record<StepId, "green" | "pending" | "active"> = {} as Record<
      StepId,
      "green" | "pending" | "active"
    >
    for (const s of STEPS) {
      const green = s.isGreen(status.status, status.finalized, localGreen, status)
      out[s.id] = green ? "green" : s.id === activeId ? "active" : "pending"
    }
    return out
  }, [status, activeId, localGreen])

  const activeStep = STEPS.find((s) => s.id === activeId) ?? STEPS[0]
  const activeIdx = STEPS.findIndex((s) => s.id === activeId)
  const allGatesGreen = status?.all_green ?? false

  const handleFinalize = useCallback(async () => {
    if (finalizing) return
    setFinalizing(true)
    setError(null)
    try {
      await finalizeBootstrap()
      // Success — the backend marker flipped. Pull the fresh status (which
      // will route us out) rather than racing the redirect ourselves.
      await reloadStatus()
      router.replace("/")
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      setError(msg)
      // Re-pull so the UI shows which gate actually blocked us.
      await reloadStatus()
    } finally {
      setFinalizing(false)
    }
  }, [finalizing, reloadStatus, router])

  const goPrev = () => {
    setUserPinned(true)
    const i = Math.max(0, activeIdx - 1)
    setActiveId(STEPS[i].id)
  }
  const goNext = () => {
    setUserPinned(true)
    const i = Math.min(STEPS.length - 1, activeIdx + 1)
    setActiveId(STEPS[i].id)
  }

  return (
    <main className="min-h-screen bg-[var(--background)] text-[var(--foreground)]">
      <div className="max-w-5xl mx-auto p-6 flex flex-col gap-4">
        <header className="flex items-center gap-3 pb-2 border-b border-[var(--border)]">
          <Rocket size={22} className="text-[var(--artifact-purple)]" />
          <div className="flex-1 min-w-0">
            <h1 className="font-mono text-base font-semibold">
              OmniSight Bootstrap
            </h1>
            <p className="font-mono text-[11px] text-[var(--muted-foreground)]">
              First-install wizard — four gates, then finalize. Close the
              wizard by turning every step green.
            </p>
          </div>
          <button
            type="button"
            onClick={() => void reloadStatus()}
            disabled={loading}
            className="font-mono text-[11px] px-2 py-1 rounded border border-[var(--border)] hover:bg-[var(--muted)]/40 disabled:opacity-40"
          >
            {loading ? "…" : "Refresh"}
          </button>
        </header>

        {error && (
          <div
            role="alert"
            className="flex items-start gap-2 p-3 rounded border border-[var(--destructive)] bg-[var(--destructive)]/10 text-[var(--destructive)] font-mono text-xs"
          >
            <AlertCircle size={14} className="shrink-0 mt-0.5" />
            <span className="break-words">{error}</span>
          </div>
        )}

        {!status ? (
          <div className="flex items-center justify-center p-12">
            <Loader2
              size={20}
              className="animate-spin text-[var(--muted-foreground)]"
            />
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-[240px_1fr] gap-4">
            <aside className="flex flex-col gap-2">
              {STEPS.map((s, i) => (
                <StepPill
                  key={s.id}
                  def={s}
                  state={stepStates[s.id] ?? "pending"}
                  active={s.id === activeId}
                  onClick={() => {
                    setUserPinned(true)
                    setActiveId(s.id)
                  }}
                  index={i}
                />
              ))}
            </aside>

            <section className="flex flex-col gap-4 p-4 rounded border border-[var(--border)] bg-[var(--card)]">
              <div className="flex items-center gap-2">
                <span className="font-mono text-[10px] tracking-wider text-[var(--muted-foreground)]">
                  STEP {activeIdx + 1} / {STEPS.length}
                </span>
                <h2 className="font-mono text-sm font-semibold">
                  {activeStep.title}
                </h2>
                {stepStates[activeId] === "green" && (
                  <span className="ml-auto flex items-center gap-1 font-mono text-[10px] text-[var(--status-green)]">
                    <Check size={12} /> complete
                  </span>
                )}
              </div>
              <p className="font-mono text-xs text-[var(--muted-foreground)]">
                {activeStep.subtitle}
              </p>

              {activeStep.id === "finalize" ? (
                <div className="flex flex-col gap-3 p-4 rounded border border-[var(--border)] bg-[var(--background)]">
                  <p className="font-mono text-xs text-[var(--foreground)]">
                    Finalizing writes <code>bootstrap_finalized=true</code> and
                    closes the wizard. The admin that calls this becomes the
                    recorded actor on <code>bootstrap_state.finalized</code>.
                  </p>
                  {!allGatesGreen && status.missing_steps.length > 0 && (
                    <p className="font-mono text-[11px] text-[var(--destructive)]">
                      Missing steps:{" "}
                      <code>{status.missing_steps.join(", ")}</code>
                    </p>
                  )}
                  <button
                    type="button"
                    data-testid="bootstrap-finalize-button"
                    onClick={() => void handleFinalize()}
                    disabled={!allGatesGreen || finalizing}
                    className="self-start flex items-center gap-2 px-3 py-2 rounded bg-[var(--artifact-purple)] text-white font-mono text-sm font-semibold hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    {finalizing ? (
                      <Loader2 size={14} className="animate-spin" />
                    ) : (
                      <Rocket size={14} />
                    )}
                    Finalize bootstrap
                  </button>
                </div>
              ) : activeStep.id === "admin_password" ? (
                <AdminPasswordStep
                  alreadyGreen={!status.status.admin_password_default}
                  onRotated={reloadStatus}
                />
              ) : activeStep.id === "init_tenant" ? (
                <InitTenantStep
                  alreadyGreen={localGreen.init_tenant === true}
                  onCompleted={() => {
                    // Optional step — flip the local-green pill AND
                    // advance the cursor so the operator sees visible
                    // progress. Mirrors the GitForgeStep pattern.
                    setLocalGreenFor("init_tenant", true)
                    const idx = STEPS.findIndex((s) => s.id === "init_tenant")
                    const next = STEPS[idx + 1]
                    if (next) {
                      setUserPinned(true)
                      setActiveId(next.id)
                    }
                  }}
                  onSkipped={() => {
                    setLocalGreenFor("init_tenant", true)
                    const idx = STEPS.findIndex((s) => s.id === "init_tenant")
                    const next = STEPS[idx + 1]
                    if (next) {
                      setUserPinned(true)
                      setActiveId(next.id)
                    }
                  }}
                />
              ) : activeStep.id === "llm_provider" ? (
                <LlmProviderStep
                  alreadyGreen={status.status.llm_provider_configured}
                  onProvisioned={reloadStatus}
                />
              ) : activeStep.id === "cf_tunnel" ? (
                <CfTunnelStep
                  alreadyGreen={status.status.cf_tunnel_configured}
                  onChanged={reloadStatus}
                />
              ) : activeStep.id === "git_forge" ? (
                <GitForgeStep
                  alreadyGreen={localGreen.git_forge === true}
                  onCompleted={() => {
                    // Clicking Skip (or Save in a token form) is an
                    // explicit "I'm done with this step" intent —
                    // flip green AND advance the cursor, even when
                    // userPinned is already true. Without the
                    // advance, the UI gives no visible feedback
                    // (the green banner is local + optional, not a
                    // finalize gate) and the operator is stuck
                    // thinking Skip is broken.
                    setLocalGreenFor("git_forge", true)
                    const idx = STEPS.findIndex(
                      (s) => s.id === "git_forge",
                    )
                    const next = STEPS[idx + 1]
                    if (next) {
                      setUserPinned(true)
                      setActiveId(next.id)
                    }
                  }}
                />
              ) : activeStep.id === "vertical_setup" ? (
                <VerticalSetupStep
                  alreadyGreen={localGreen.vertical_setup === true}
                  onCompleted={() => {
                    // BS.9.2 — same explicit-advance pattern as
                    // GitForgeStep: flip ``localGreen`` AND advance
                    // the cursor on Skip. Skipping issues no backend
                    // call, so ``_verticals_chosen()`` stays False
                    // and the operator can return to pick verticals
                    // before finalize if they change their mind.
                    setLocalGreenFor("vertical_setup", true)
                    const idx = STEPS.findIndex(
                      (s) => s.id === "vertical_setup",
                    )
                    const next = STEPS[idx + 1]
                    if (next) {
                      setUserPinned(true)
                      setActiveId(next.id)
                    }
                  }}
                />
              ) : activeStep.id === "services_ready" ? (
                <ServiceHealthStep
                  onChanged={(allGreen) =>
                    setLocalGreenFor("services_ready", allGreen)
                  }
                />
              ) : activeStep.id === "frontend_freshness" ? (
                <FrontendFreshnessStep
                  freshness={status.frontend_freshness}
                />
              ) : activeStep.id === "smoke" ? (
                <SmokeSubsetStep
                  alreadyGreen={status.status.smoke_passed}
                  onPassed={reloadStatus}
                  onJumpToStep={(id) => {
                    setUserPinned(true)
                    setActiveId(id)
                  }}
                  finalize={{
                    allGatesGreen,
                    busy: finalizing,
                    missingSteps: status.missing_steps,
                    onFinalize: () => void handleFinalize(),
                  }}
                />
              ) : (
                <StepBodyPlaceholder step={activeStep} />
              )}

              <div className="flex items-center justify-between pt-2 border-t border-[var(--border)]">
                <button
                  type="button"
                  onClick={goPrev}
                  disabled={activeIdx === 0}
                  className="flex items-center gap-1 font-mono text-xs px-2 py-1 rounded border border-[var(--border)] hover:bg-[var(--muted)]/40 disabled:opacity-30"
                >
                  <ChevronLeft size={12} /> Back
                </button>
                <button
                  type="button"
                  onClick={goNext}
                  disabled={activeIdx === STEPS.length - 1}
                  className="flex items-center gap-1 font-mono text-xs px-2 py-1 rounded border border-[var(--border)] hover:bg-[var(--muted)]/40 disabled:opacity-30"
                >
                  Next <ChevronRight size={12} />
                </button>
              </div>
            </section>
          </div>
        )}

        <footer className="pt-2 font-mono text-[10px] text-[var(--muted-foreground)]">
          Backend gates:
          {status ? (
            <span className="ml-1">
              admin_password_default={String(status.status.admin_password_default)} ·
              llm_provider_configured={String(status.status.llm_provider_configured)} ·
              cf_tunnel_configured={String(status.status.cf_tunnel_configured)} ·
              smoke_passed={String(status.status.smoke_passed)} ·
              finalized={String(status.finalized)}
            </span>
          ) : (
            <span className="ml-1">(loading)</span>
          )}
        </footer>
      </div>
    </main>
  )
}
