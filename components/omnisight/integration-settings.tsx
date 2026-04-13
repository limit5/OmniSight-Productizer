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
          <button
            onClick={(e) => { e.stopPropagation(); handleTest() }}
            className="px-1.5 py-0.5 rounded text-[8px] font-mono bg-[var(--neural-blue)]/10 text-[var(--neural-blue)] hover:bg-[var(--neural-blue)]/20 transition-colors"
          >
            TEST
          </button>
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
              {testResult.user && ` (${testResult.user})`}
              {testResult.version && ` — ${testResult.version}`}
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

  useEffect(() => {
    if (open) {
      api.getSettings().then(setSettingsData).catch(() => {})
    }
  }, [open])

  const getVal = (category: string, field: string): string => {
    const configKey = `${category === "llm" ? "llm" : category === "git" ? "" : category === "gerrit" ? "gerrit" : category === "jira" ? "notification_jira" : category === "slack" ? "notification_slack" : ""}_${field}`
    if (configKey in dirty) return String(dirty[configKey])
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
            <SettingField label="Provider" value={getVal("llm", "provider")} onChange={v => setVal("llm_provider", v)} />
            <SettingField label="Model" value={getVal("llm", "model")} onChange={v => setVal("llm_model", v)} />
            <div className="pt-1 pb-0.5">
              <span className="font-mono text-[8px] text-[var(--muted-foreground)] uppercase tracking-wider">API Keys (leave empty to disable)</span>
            </div>
            <SettingField label="Anthropic" value={getVal("llm", "anthropic_api_key")} type="password" onChange={v => setVal("anthropic_api_key", v)} />
            <SettingField label="OpenAI" value={getVal("llm", "openai_api_key")} type="password" onChange={v => setVal("openai_api_key", v)} />
            <SettingField label="Google" value={getVal("llm", "google_api_key")} type="password" onChange={v => setVal("google_api_key", v)} />
            <SettingField label="OpenRouter" value={getVal("llm", "openrouter_api_key")} type="password" onChange={v => setVal("openrouter_api_key", v)} />
            <SettingField label="xAI (Grok)" value={getVal("llm", "xai_api_key")} type="password" onChange={v => setVal("xai_api_key", v)} />
            <SettingField label="Groq" value={getVal("llm", "groq_api_key")} type="password" onChange={v => setVal("groq_api_key", v)} />
            <SettingField label="DeepSeek" value={getVal("llm", "deepseek_api_key")} type="password" onChange={v => setVal("deepseek_api_key", v)} />
            <SettingField label="Together" value={getVal("llm", "together_api_key")} type="password" onChange={v => setVal("together_api_key", v)} />
            <SettingField label="Ollama URL" value={getVal("llm", "ollama_base_url")} onChange={v => setVal("ollama_base_url", v)} />
            <div className="pt-1">
              <SettingField label="Fallback" value={getVal("llm", "fallback_chain")} onChange={v => setVal("llm_fallback_chain", v)} />
            </div>
          </SettingsSection>

          <SettingsSection title="GIT & SSH" integration="ssh">
            <SettingField label="SSH Key" value={getVal("git", "ssh_key_path")} onChange={v => setVal("git_ssh_key_path", v)} />
            <SettingField label="GitHub Token" value={getVal("git", "github_token")} type="password" onChange={v => setVal("github_token", v)} />
            <SettingField label="GitLab Token" value={getVal("git", "gitlab_token")} type="password" onChange={v => setVal("gitlab_token", v)} />
            <SettingField label="GitLab URL" value={getVal("git", "gitlab_url")} onChange={v => setVal("gitlab_url", v)} />
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
            <SettingField label="Secret" value={getVal("webhooks", "github_secret")} type="password" onChange={v => setVal("github_webhook_secret", v)} />
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
