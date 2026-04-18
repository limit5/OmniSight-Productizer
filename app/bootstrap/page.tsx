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
  Loader2,
  RefreshCw,
  Rocket,
  Shield,
  KeyRound,
  Cloud,
  FlaskConical,
  Sparkles,
  Bot,
  Server,
  Cpu,
  GitBranch,
} from "lucide-react"
import {
  BOOTSTRAP_ADMIN_PASSWORD_KIND_COPY,
  BOOTSTRAP_PROVIDER_KEY_URL,
  BOOTSTRAP_PROVISION_KIND_COPY,
  BOOTSTRAP_START_SERVICES_KIND_COPY,
  BootstrapAdminPasswordError,
  BootstrapLlmProvisionError,
  BootstrapStartServicesError,
  bootstrapCfTunnelSkip,
  bootstrapDetectOllama,
  bootstrapLlmProvision,
  bootstrapParallelHealthCheck,
  bootstrapSetAdminPassword,
  bootstrapSmokeSubset,
  bootstrapStartServices,
  finalizeBootstrap,
  getBootstrapStatus,
  testGitForgeToken,
  updateSettings,
  type GitForgeTokenTestResult,
  type BootstrapAdminPasswordKind,
  type BootstrapGates,
  type BootstrapHealthCheckResult,
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

// ─── Step definitions ────────────────────────────────────────────────

type StepId =
  | "admin_password"
  | "llm_provider"
  | "cf_tunnel"
  | "git_forge"
  | "services_ready"
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
    id: "services_ready",
    title: "Service Health",
    subtitle: "Verify backend / frontend / DB / tunnel are all live",
    icon: Activity,
    isGreen: (_g, _finalized, localGreen) => localGreen.services_ready === true,
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
          <p className="font-mono text-[10px] text-[var(--muted-foreground)] leading-relaxed">
            The <strong>token / URL entry</strong> and{" "}
            <strong>Test Connection</strong> controls for{" "}
            {activeDef.label} land in the next B14 row. For now, switch
            tabs to preview each provider, or skip below and configure
            later from <strong>Settings → Integration</strong>.
          </p>
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
}: {
  row: HealthRow
  result: BootstrapHealthCheckResult | undefined
}) {
  const Icon = row.icon
  // Tri-state: undefined while we have no observation yet, then driven
  // by the latest probe. ``skipped`` counts as green (LAN-only).
  const status = result?.status ?? "pending"
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
      </div>
      <p className="font-mono text-[11px] text-[var(--muted-foreground)] leading-relaxed">
        If a probe above is stuck red, kick the launcher from here. The
        backend auto-detects the deploy mode (systemd / docker-compose /
        dev) — a systemctl failure surfaces the exact kind (missing
        sudoers, unit not installed, binary not on PATH, timeout) with a
        targeted remediation hint.
      </p>
      <div className="flex items-center gap-2">
        <button
          type="button"
          data-testid="bootstrap-start-services-button"
          onClick={() => void run()}
          disabled={busy}
          className="flex items-center gap-1 font-mono text-[11px] px-2 py-1 rounded border border-[var(--border)] hover:bg-[var(--muted)]/40 disabled:opacity-40"
        >
          {busy ? (
            <Loader2 size={12} className="animate-spin" />
          ) : (
            <Rocket size={12} />
          )}
          Launch services
        </button>
        {okResult && (
          <span
            data-testid="bootstrap-start-services-ok"
            data-status={okResult.status}
            data-mode={okResult.mode}
            className="font-mono text-[11px] text-[var(--status-green)]"
          >
            <Check size={12} className="inline mr-1" />
            {okResult.status === "already_running"
              ? "already running (dev mode)"
              : `launched (${okResult.mode}, rc=${okResult.returncode})`}
          </span>
        )}
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
      setSnapshot(next)
      setProbeCount((n) => n + 1)
      onChangedRef.current(next.all_green)
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      setError(msg)
      onChangedRef.current(false)
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
          />
        ))}
      </div>

      <StartServicesPanel
        anyRed={
          snapshot != null &&
          HEALTH_ROWS.some((row) => snapshot[row.id]?.status === "red")
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

  const run = useCallback(async () => {
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
          onClick={() => void run()}
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
    services_ready: {
      gate: "parallel-health-check.all_green === true",
      todo: "L5 — backend/frontend/DB/CF connector live ticks",
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
  const out = {} as Record<StepId, boolean>
  for (const s of STEPS) out[s.id] = false
  // Optional (non-gate) steps seed-green so auto-advance doesn't stall
  // on them and finalize is never blocked. Operators can still click
  // the sidebar pill to open and configure these steps explicitly.
  out.git_forge = true
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
      (s) => !s.isGreen(status.status, status.finalized, localGreen),
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
      const green = s.isGreen(status.status, status.finalized, localGreen)
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
                  onCompleted={() => setLocalGreenFor("git_forge", true)}
                />
              ) : activeStep.id === "services_ready" ? (
                <ServiceHealthStep
                  onChanged={(allGreen) =>
                    setLocalGreenFor("services_ready", allGreen)
                  }
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
