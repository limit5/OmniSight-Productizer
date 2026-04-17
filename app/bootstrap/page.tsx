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

import { useCallback, useEffect, useMemo, useState } from "react"
import { useRouter } from "next/navigation"
import {
  AlertCircle,
  Check,
  ChevronLeft,
  ChevronRight,
  CircleDashed,
  Loader2,
  Rocket,
  Shield,
  KeyRound,
  Cloud,
  FlaskConical,
  Sparkles,
  Bot,
  Server,
  Cpu,
} from "lucide-react"
import {
  BOOTSTRAP_PROVISION_KIND_COPY,
  BootstrapLlmProvisionError,
  bootstrapDetectOllama,
  bootstrapLlmProvision,
  bootstrapSetAdminPassword,
  finalizeBootstrap,
  getBootstrapStatus,
  type BootstrapGates,
  type BootstrapLlmProvisionKind,
  type BootstrapLlmProvisionRequest,
  type BootstrapLlmProvisionResponse,
  type BootstrapOllamaDetectResponse,
  type BootstrapStatusResponse,
} from "@/lib/api"
import {
  estimatePasswordStrength,
  PASSWORD_MIN_LENGTH,
  PASSWORD_MIN_SCORE,
} from "@/lib/password_strength"

// ─── Step definitions ────────────────────────────────────────────────

type StepId =
  | "admin_password"
  | "llm_provider"
  | "cf_tunnel"
  | "smoke"
  | "finalize"

interface StepDef {
  id: StepId
  title: string
  subtitle: string
  icon: React.ComponentType<{ size?: number; className?: string }>
  /** Returns true if this step is satisfied by current backend signals. */
  isGreen: (g: BootstrapGates, finalized: boolean) => boolean
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
      setSuccess(false)
      try {
        await bootstrapSetAdminPassword(currentPassword, newPassword)
        setSuccess(true)
        setCurrentPassword("")
        setNewPassword("")
        setConfirmPassword("")
        await onRotated()
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err)
        setLocalError(msg)
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
      {localError && (
        <p
          role="alert"
          data-testid="bootstrap-admin-password-error"
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
}: {
  kind: BootstrapLlmProvisionKind
  detail: string
}) {
  const copy = BOOTSTRAP_PROVISION_KIND_COPY[kind]
  return (
    <div
      role="alert"
      data-testid="bootstrap-llm-provider-error"
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

      {errKind && <ProvisionErrorBanner kind={errKind} detail={errDetail} />}

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

export default function BootstrapPage() {
  const router = useRouter()
  const [status, setStatus] = useState<BootstrapStatusResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [activeId, setActiveId] = useState<StepId>("admin_password")
  const [finalizing, setFinalizing] = useState(false)

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
    const firstRed = STEPS.find((s) => !s.isGreen(status.status, status.finalized))
    if (firstRed) setActiveId(firstRed.id)
  }, [status, userPinned])

  const stepStates = useMemo(() => {
    if (!status) return {} as Record<StepId, "green" | "pending" | "active">
    const out: Record<StepId, "green" | "pending" | "active"> = {} as Record<
      StepId,
      "green" | "pending" | "active"
    >
    for (const s of STEPS) {
      const green = s.isGreen(status.status, status.finalized)
      out[s.id] = green ? "green" : s.id === activeId ? "active" : "pending"
    }
    return out
  }, [status, activeId])

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
