"use client"

import { useState, useEffect, useCallback } from "react"
import { createPortal } from "react-dom"
import { Settings, X, Check, AlertTriangle, Loader, ChevronDown, ChevronUp, Wifi, WifiOff } from "lucide-react"
import * as api from "@/lib/api"

interface IntegrationSettingsProps {
  open: boolean
  onClose: () => void
}

interface TestResult {
  status: string
  message?: string
  [key: string]: unknown
}

const STATUS_ICON = {
  ok: <Check size={12} className="text-[var(--validation-emerald)]" />,
  error: <AlertTriangle size={12} className="text-[var(--critical-red)]" />,
  not_configured: <WifiOff size={12} className="text-[var(--muted-foreground)]" />,
  testing: <Loader size={12} className="animate-spin text-[var(--neural-blue)]" />,
}

function SettingsSection({ title, integration, children }: {
  title: string
  integration?: string
  children: React.ReactNode
}) {
  const [expanded, setExpanded] = useState(true)
  const [testResult, setTestResult] = useState<TestResult | null>(null)
  const [testing, setTesting] = useState(false)

  const handleTest = useCallback(async () => {
    if (!integration) return
    setTesting(true)
    setTestResult(null)
    try {
      const result = await api.testIntegration(integration)
      setTestResult(result)
    } catch (e) {
      setTestResult({ status: "error", message: String(e) })
    } finally {
      setTesting(false)
    }
  }, [integration])

  return (
    <div className="border border-[var(--border)] rounded-md overflow-hidden">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-2 px-3 py-2 bg-[var(--secondary)] hover:bg-[var(--secondary)]/80 transition-colors"
      >
        <span className="font-mono text-[10px] font-bold text-[var(--neural-blue)] flex-1 text-left">{title}</span>
        {testResult && !testing && STATUS_ICON[testResult.status as keyof typeof STATUS_ICON]}
        {testing && STATUS_ICON.testing}
        {integration && !testing && (
          <span
            role="button"
            tabIndex={0}
            onClick={(e) => { e.stopPropagation(); handleTest() }}
            onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.stopPropagation(); handleTest() } }}
            className="px-1.5 py-0.5 rounded text-[8px] font-mono bg-[var(--neural-blue)]/10 text-[var(--neural-blue)] hover:bg-[var(--neural-blue)]/20 transition-colors cursor-pointer"
          >
            TEST
          </span>
        )}
        {expanded ? <ChevronUp size={10} className="text-[var(--muted-foreground)]" /> : <ChevronDown size={10} className="text-[var(--muted-foreground)]" />}
      </button>
      {expanded && (
        <div className="px-3 py-2 space-y-1.5">
          {children}
          {testResult && (
            <div className={`font-mono text-[9px] px-2 py-1 rounded ${
              testResult.status === "ok" ? "bg-[var(--validation-emerald)]/10 text-[var(--validation-emerald)]"
              : testResult.status === "not_configured" ? "bg-[var(--secondary)] text-[var(--muted-foreground)]"
              : "bg-[var(--critical-red)]/10 text-[var(--critical-red)]"
            }`}>
              {testResult.status === "ok" ? "Connected" : testResult.message || testResult.status}
              {/* user / version are open-ended metadata on the integration
                  probe response (unknown-typed); coerce to string before
                  rendering so React's ReactNode contract stays satisfied. */}
              {testResult.user ? ` (${String(testResult.user)})` : null}
              {testResult.version ? ` — ${String(testResult.version)}` : null}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function SettingField({ label, value, type = "text", onChange }: {
  label: string; value: string; type?: string; onChange: (v: string) => void
}) {
  return (
    <div className="flex items-center gap-2">
      <label className="font-mono text-[9px] text-[var(--muted-foreground)] w-20 shrink-0">{label}</label>
      <input
        type={type}
        value={value}
        onChange={e => onChange(e.target.value)}
        className="flex-1 font-mono text-[10px] px-2 py-1 rounded bg-[var(--background)] border border-[var(--border)] text-[var(--foreground)] focus:outline-none focus:border-[var(--neural-blue)]"
      />
    </div>
  )
}

function ToggleField({ label, value, onChange }: {
  label: string; value: boolean; onChange: (v: boolean) => void
}) {
  return (
    <div className="flex items-center gap-2">
      <label className="font-mono text-[9px] text-[var(--muted-foreground)] w-20 shrink-0">{label}</label>
      <button
        onClick={() => onChange(!value)}
        className={`px-2 py-0.5 rounded font-mono text-[9px] transition-colors ${
          value ? "bg-[var(--validation-emerald)]/20 text-[var(--validation-emerald)]" : "bg-[var(--secondary)] text-[var(--muted-foreground)]"
        }`}
      >
        {value ? "ON" : "OFF"}
      </button>
    </div>
  )
}

export function IntegrationSettings({ open, onClose }: IntegrationSettingsProps) {
  const [settingsData, setSettingsData] = useState<Record<string, Record<string, unknown>>>({})
  const [dirty, setDirty] = useState<Record<string, string | number | boolean>>({})
  const [saving, setSaving] = useState(false)
  const [providers, setProviders] = useState<api.ProviderConfig[]>([])

  useEffect(() => {
    if (open) {
      setDirty({})  // Clear unsaved changes on fresh open
      api.getSettings().then(setSettingsData).catch(() => {})
      api.getProviders().then(r => setProviders(r.providers)).catch(() => {})
    }
  }, [open])

  const getVal = (category: string, field: string, dirtyKey?: string): string => {
    // dirtyKey: the exact key used in setVal() — allows override for fields
    // where the config key doesn't follow category_field pattern
    const key = dirtyKey || `${
      category === "llm" ? "llm" :
      category === "git" ? "git" :
      category === "gerrit" ? "gerrit" :
      category === "jira" ? "notification_jira" :
      category === "slack" ? "notification_slack" :
      category === "webhooks" ? "github_webhook" : ""
    }_${field}`
    if (key in dirty) return String(dirty[key])
    return String(settingsData[category]?.[field] ?? "")
  }

  const setVal = (configKey: string, value: string | boolean) => {
    setDirty(prev => ({ ...prev, [configKey]: value }))
  }

  const handleSave = async () => {
    if (Object.keys(dirty).length === 0) return
    setSaving(true)
    try {
      await api.updateSettings(dirty)
      setDirty({})
      // Reload settings
      const fresh = await api.getSettings()
      setSettingsData(fresh)
    } catch (e) {
      console.error("Save failed:", e)
    } finally {
      setSaving(false)
    }
  }

  if (!open || typeof document === "undefined") return null

  return createPortal(
    <div className="fixed inset-0 z-[100] flex items-center justify-center">
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={onClose} />

      {/* Modal */}
      <div className="relative z-10 w-full max-w-lg max-h-[80vh] m-4 bg-[var(--card)] border border-[var(--border)] rounded-lg shadow-2xl flex flex-col overflow-hidden">
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-[var(--border)] bg-[var(--secondary)]">
          <div className="flex items-center gap-2">
            <Settings size={14} className="text-[var(--neural-blue)]" />
            <h2 className="font-sans text-sm font-semibold tracking-fui text-[var(--neural-blue)]">SYSTEM INTEGRATIONS</h2>
          </div>
          <button onClick={onClose} className="p-1 rounded hover:bg-[var(--background)] transition-colors">
            <X size={14} className="text-[var(--muted-foreground)]" />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto overflow-x-hidden p-3 space-y-2">

          <SettingsSection title="LLM PROVIDERS">
            {/* Active Provider — dropdown */}
            {(() => {
              const currentProvider = (dirty["llm_provider"] as string) ?? String(settingsData["llm"]?.["provider"] ?? "")
              const selectedProvider = providers.find(p => p.id === currentProvider)
              const modelList = selectedProvider?.models ?? []
              const currentModel = (dirty["llm_model"] as string) ?? String(settingsData["llm"]?.["model"] ?? "")
              return (
                <>
                  <div className="flex items-center gap-2">
                    <label className="font-mono text-[9px] text-[var(--muted-foreground)] w-20 shrink-0">Provider</label>
                    <select
                      value={currentProvider}
                      onChange={e => {
                        setVal("llm_provider", e.target.value)
                        // Auto-select default model for new provider
                        const p = providers.find(pr => pr.id === e.target.value)
                        if (p) setVal("llm_model", p.default_model)
                      }}
                      className="flex-1 font-mono text-[10px] px-2 py-1 rounded bg-[var(--background)] border border-[var(--border)] text-[var(--foreground)] focus:outline-none focus:border-[var(--neural-blue)]"
                    >
                      {providers.map(p => (
                        <option key={p.id} value={p.id}>
                          {p.name} {p.configured ? "✅" : "⚫"}
                        </option>
                      ))}
                    </select>
                  </div>
                  {/* Active Model — dropdown linked to provider */}
                  <div className="flex items-center gap-2">
                    <label className="font-mono text-[9px] text-[var(--muted-foreground)] w-20 shrink-0">Model</label>
                    <select
                      value={currentModel}
                      onChange={e => setVal("llm_model", e.target.value)}
                      className="flex-1 font-mono text-[10px] px-2 py-1 rounded bg-[var(--background)] border border-[var(--border)] text-[var(--foreground)] focus:outline-none focus:border-[var(--neural-blue)]"
                    >
                      {modelList.map(m => (
                        <option key={m} value={m}>{m}</option>
                      ))}
                      {/* Allow current model even if not in list */}
                      {currentModel && !modelList.includes(currentModel) && (
                        <option value={currentModel}>{currentModel} (custom)</option>
                      )}
                    </select>
                  </div>
                </>
              )
            })()}
            {/* API Keys with status indicators */}
            <div className="pt-1.5 pb-0.5">
              <span className="font-mono text-[8px] text-[var(--muted-foreground)] uppercase tracking-wider">API Keys</span>
            </div>
            {[
              { id: "anthropic", label: "Anthropic", key: "anthropic_api_key" },
              { id: "openai", label: "OpenAI", key: "openai_api_key" },
              { id: "google", label: "Google", key: "google_api_key" },
              { id: "openrouter", label: "OpenRouter", key: "openrouter_api_key" },
              { id: "xai", label: "xAI (Grok)", key: "xai_api_key" },
              { id: "groq", label: "Groq", key: "groq_api_key" },
              { id: "deepseek", label: "DeepSeek", key: "deepseek_api_key" },
              { id: "together", label: "Together", key: "together_api_key" },
            ].map(({ id, label, key }) => {
              const configured = providers.find(p => p.id === id)?.configured ?? false
              const hasLocalEdit = key in dirty && String(dirty[key]).length > 0
              const showConfigured = configured || hasLocalEdit
              return (
                <div key={id} className="flex items-center gap-2">
                  <label className="font-mono text-[9px] text-[var(--muted-foreground)] w-20 shrink-0 flex items-center gap-1">
                    <span className={`w-1.5 h-1.5 rounded-full ${showConfigured ? "bg-[var(--validation-emerald)]" : "bg-[var(--muted-foreground)]/30"}`} />
                    {label}
                  </label>
                  <input
                    type="password"
                    value={key in dirty ? String(dirty[key]) : String(settingsData["llm"]?.[key] ?? "")}
                    onChange={e => setVal(key, e.target.value)}
                    placeholder={configured ? "••• configured •••" : "paste key here"}
                    className="flex-1 font-mono text-[10px] px-2 py-1 rounded bg-[var(--background)] border border-[var(--border)] text-[var(--foreground)] placeholder:text-[var(--muted-foreground)]/50 focus:outline-none focus:border-[var(--neural-blue)]"
                  />
                </div>
              )
            })}
            <div className="flex items-center gap-2">
              <label className="font-mono text-[9px] text-[var(--muted-foreground)] w-20 shrink-0 flex items-center gap-1">
                <span className={`w-1.5 h-1.5 rounded-full ${providers.find(p => p.id === "ollama")?.configured ? "bg-[var(--validation-emerald)]" : "bg-[var(--muted-foreground)]/30"}`} />
                Ollama
              </label>
              <input
                type="text"
                value={"ollama_base_url" in dirty ? String(dirty["ollama_base_url"]) : String(settingsData["llm"]?.["ollama_base_url"] ?? "http://localhost:11434")}
                onChange={e => setVal("ollama_base_url", e.target.value)}
                className="flex-1 font-mono text-[10px] px-2 py-1 rounded bg-[var(--background)] border border-[var(--border)] text-[var(--foreground)] focus:outline-none focus:border-[var(--neural-blue)]"
              />
            </div>
            <div className="pt-1">
              <SettingField label="Fallback" value={getVal("llm", "fallback_chain")} onChange={v => setVal("llm_fallback_chain", v)} />
            </div>
          </SettingsSection>

          <SettingsSection title="GIT REPOSITORIES" integration="ssh">
            {/* Credential Registry — per-repo entries */}
            {(() => {
              const creds = (settingsData["git"]?.["credentials"] as Array<Record<string, unknown>>) || []
              return creds.length > 0 ? (
                <div className="space-y-2">
                  {creds.map((cred, i) => {
                    const platform = String(cred.platform || "unknown")
                    const platformColor = platform === "github" ? "var(--validation-emerald)" :
                      platform === "gitlab" ? "var(--hardware-orange)" :
                      platform === "gerrit" ? "var(--neural-blue)" : "var(--muted-foreground)"
                    const hasToken = String(cred.token || "") !== "" && String(cred.token || "") !== "***"
                    return (
                      <div key={String(cred.id || i)} className="p-2 rounded border border-[var(--border)] bg-[var(--background)] space-y-1">
                        <div className="flex items-center gap-2">
                          <span className={`w-1.5 h-1.5 rounded-full`} style={{ backgroundColor: hasToken || cred.has_secret ? "var(--validation-emerald)" : "var(--muted-foreground)" }} />
                          <span className="font-mono text-[10px] font-semibold" style={{ color: platformColor }}>
                            {platform.toUpperCase()}
                          </span>
                          <span className="font-mono text-[9px] text-[var(--muted-foreground)] flex-1 truncate">
                            {String(cred.id || "")}
                          </span>
                        </div>
                        <div className="font-mono text-[9px] text-[var(--muted-foreground)] truncate pl-3.5">
                          {String(cred.url || cred.ssh_host || "")}
                          {cred.project ? ` / ${cred.project}` : ""}
                        </div>
                      </div>
                    )
                  })}
                </div>
              ) : (
                <div className="font-mono text-[9px] text-[var(--muted-foreground)] py-1 opacity-60">
                  No repositories configured. Edit configs/git_credentials.yaml or use fields below.
                </div>
              )
            })()}
            {/* Legacy scalar fallback fields */}
            <div className="pt-1.5 pb-0.5">
              <span className="font-mono text-[8px] text-[var(--muted-foreground)] uppercase tracking-wider">Default Credentials (fallback)</span>
            </div>
            <SettingField label="SSH Key" value={getVal("git", "ssh_key_path")} onChange={v => setVal("git_ssh_key_path", v)} />
            <SettingField label="GitHub Token" value={getVal("git", "github_token", "github_token")} type="password" onChange={v => setVal("github_token", v)} />
            <SettingField label="GitLab Token" value={getVal("git", "gitlab_token", "gitlab_token")} type="password" onChange={v => setVal("gitlab_token", v)} />
            <SettingField label="GitLab URL" value={getVal("git", "gitlab_url", "gitlab_url")} onChange={v => setVal("gitlab_url", v)} />
          </SettingsSection>

          <SettingsSection title="GERRIT CODE REVIEW" integration="gerrit">
            <ToggleField label="Enabled" value={getVal("gerrit", "enabled") === "true"} onChange={v => setVal("gerrit_enabled", v)} />
            <SettingField label="URL" value={getVal("gerrit", "url")} onChange={v => setVal("gerrit_url", v)} />
            <SettingField label="SSH Host" value={getVal("gerrit", "ssh_host")} onChange={v => setVal("gerrit_ssh_host", v)} />
            <SettingField label="SSH Port" value={getVal("gerrit", "ssh_port")} onChange={v => setVal("gerrit_ssh_port", v)} />
            <SettingField label="Project" value={getVal("gerrit", "project")} onChange={v => setVal("gerrit_project", v)} />
          </SettingsSection>

          <SettingsSection title="JIRA ISSUE TRACKING" integration="jira">
            <SettingField label="URL" value={getVal("jira", "url")} onChange={v => setVal("notification_jira_url", v)} />
            <SettingField label="Token" value={getVal("jira", "token")} type="password" onChange={v => setVal("notification_jira_token", v)} />
            <SettingField label="Project Key" value={getVal("jira", "project")} onChange={v => setVal("notification_jira_project", v)} />
          </SettingsSection>

          <SettingsSection title="SLACK NOTIFICATIONS" integration="slack">
            <SettingField label="Webhook" value={getVal("slack", "webhook")} type="password" onChange={v => setVal("notification_slack_webhook", v)} />
            <SettingField label="Mention ID" value={getVal("slack", "mention")} onChange={v => setVal("notification_slack_mention", v)} />
          </SettingsSection>

          <SettingsSection title="GITHUB WEBHOOK" integration="github">
            <SettingField label="Secret" value={getVal("webhooks", "github_secret", "github_webhook_secret")} type="password" onChange={v => setVal("github_webhook_secret", v)} />
          </SettingsSection>

        </div>

        {/* Footer */}
        <div className="flex items-center justify-between px-4 py-2 border-t border-[var(--border)] bg-[var(--secondary)]">
          <span className="font-mono text-[9px] text-[var(--muted-foreground)]">
            {Object.keys(dirty).length > 0 ? `${Object.keys(dirty).length} unsaved change(s)` : "Runtime only — resets on restart"}
          </span>
          <div className="flex gap-2">
            <button
              onClick={() => setDirty({})}
              disabled={Object.keys(dirty).length === 0}
              className="px-3 py-1 rounded font-mono text-[10px] border border-[var(--border)] text-[var(--muted-foreground)] hover:bg-[var(--background)] disabled:opacity-30 transition-colors"
            >
              DISCARD
            </button>
            <button
              onClick={handleSave}
              disabled={Object.keys(dirty).length === 0 || saving}
              className="px-3 py-1 rounded font-mono text-[10px] bg-[var(--neural-blue)] text-black font-semibold hover:opacity-90 disabled:opacity-30 transition-colors"
            >
              {saving ? "SAVING..." : "SAVE & APPLY"}
            </button>
          </div>
        </div>
      </div>
    </div>,
    document.body
  )
}

/** Trigger button for the header */
export function SettingsButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className="p-1.5 rounded hover:bg-[var(--neural-blue)]/10 transition-colors"
      title="System Integrations"
    >
      <Settings size={14} className="text-[var(--muted-foreground)] hover:text-[var(--neural-blue)]" />
    </button>
  )
}
