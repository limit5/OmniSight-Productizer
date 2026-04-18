"use client"

import { useState, useEffect, useCallback } from "react"
import { createPortal } from "react-dom"
import { Settings, X, Check, AlertTriangle, Loader, ChevronDown, ChevronUp, WifiOff, Key, Plus, Trash2, HardDrive, RefreshCw } from "lucide-react"
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

const SECRET_TYPES = ["git_credential", "provider_key", "cloudflare_token", "webhook_secret", "custom"] as const

function TenantSecretsSection({ settingsData }: { settingsData: Record<string, Record<string, unknown>> }) {
  const tenantSecrets = settingsData["tenant_secrets"] as { tenant_id?: string; secrets?: Record<string, Array<{ id: string; key_name: string; fingerprint: string; metadata: Record<string, unknown>; updated_at: string }>> } | undefined
  const tid = tenantSecrets?.tenant_id ?? "t-default"
  const secrets = tenantSecrets?.secrets ?? {}

  const [adding, setAdding] = useState(false)
  const [newType, setNewType] = useState<string>("provider_key")
  const [newName, setNewName] = useState("")
  const [newValue, setNewValue] = useState("")
  const [saving, setSaving] = useState(false)

  const handleAdd = useCallback(async () => {
    if (!newName || !newValue) return
    setSaving(true)
    try {
      await api.createTenantSecret({ key_name: newName, value: newValue, secret_type: newType })
      setNewName("")
      setNewValue("")
      setAdding(false)
      window.location.reload()
    } catch (e) {
      console.error("Failed to add secret:", e)
    } finally {
      setSaving(false)
    }
  }, [newName, newValue, newType])

  const handleDelete = useCallback(async (id: string) => {
    try {
      await api.deleteTenantSecret(id)
      window.location.reload()
    } catch (e) {
      console.error("Failed to delete secret:", e)
    }
  }, [])

  const allSecrets = Object.entries(secrets).flatMap(([type, items]) =>
    items.map(item => ({ ...item, secret_type: type }))
  )

  return (
    <SettingsSection title={`TENANT SECRETS — ${tid}`}>
      {allSecrets.length > 0 ? (
        <div className="space-y-1">
          {allSecrets.map(s => (
            <div key={s.id} className="flex items-center gap-2 p-1.5 rounded border border-[var(--border)] bg-[var(--background)]">
              <Key size={10} className="text-[var(--hardware-orange)] shrink-0" />
              <span className="font-mono text-[9px] px-1 py-0.5 rounded bg-[var(--neural-blue)]/10 text-[var(--neural-blue)]">
                {s.secret_type}
              </span>
              <span className="font-mono text-[10px] text-[var(--foreground)] flex-1 truncate">{s.key_name}</span>
              <span className="font-mono text-[9px] text-[var(--muted-foreground)]">{s.fingerprint}</span>
              <button
                onClick={() => handleDelete(s.id)}
                className="p-0.5 rounded hover:bg-[var(--critical-red)]/10 transition-colors"
                title="Delete secret"
              >
                <Trash2 size={10} className="text-[var(--muted-foreground)] hover:text-[var(--critical-red)]" />
              </button>
            </div>
          ))}
        </div>
      ) : (
        <div className="font-mono text-[9px] text-[var(--muted-foreground)] py-1 opacity-60">
          No tenant-scoped secrets. Add credentials below.
        </div>
      )}

      {adding ? (
        <div className="mt-2 p-2 rounded border border-[var(--neural-blue)]/30 bg-[var(--secondary)] space-y-1.5">
          <div className="flex items-center gap-2">
            <label className="font-mono text-[9px] text-[var(--muted-foreground)] w-14 shrink-0">Type</label>
            <select
              value={newType}
              onChange={e => setNewType(e.target.value)}
              className="flex-1 font-mono text-[10px] px-2 py-1 rounded bg-[var(--background)] border border-[var(--border)] text-[var(--foreground)]"
            >
              {SECRET_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
            </select>
          </div>
          <SettingField label="Name" value={newName} onChange={setNewName} />
          <SettingField label="Value" value={newValue} type="password" onChange={setNewValue} />
          <div className="flex gap-2 justify-end">
            <button onClick={() => setAdding(false)} className="px-2 py-0.5 rounded font-mono text-[9px] text-[var(--muted-foreground)] hover:bg-[var(--background)]">
              CANCEL
            </button>
            <button
              onClick={handleAdd}
              disabled={!newName || !newValue || saving}
              className="px-2 py-0.5 rounded font-mono text-[9px] bg-[var(--neural-blue)] text-black font-semibold disabled:opacity-30"
            >
              {saving ? "SAVING..." : "ADD"}
            </button>
          </div>
        </div>
      ) : (
        <button
          onClick={() => setAdding(true)}
          className="mt-1 flex items-center gap-1 px-2 py-1 rounded font-mono text-[9px] text-[var(--neural-blue)] hover:bg-[var(--neural-blue)]/10 transition-colors"
        >
          <Plus size={10} /> Add Secret
        </button>
      )}
    </SettingsSection>
  )
}

/** B14 Part B rows 1-5 — collapsible "Multiple Instances" sub-area inside
 *  the GIT REPOSITORIES block. Row 1 delivered the expandable scaffold;
 *  row 2 added "Add GitHub Instance" (hostname + token); row 3 added the
 *  parallel "Add GitLab Instance" (URL + token); row 4 wired the instance
 *  list + per-row TEST / REMOVE buttons; row 5 (this revision) pipes
 *  mutations into the parent's `dirty` state so SAVE & APPLY serialises
 *  the list into `github_token_map` / `gitlab_token_map` JSON (the
 *  in-memory settings fields whose env-var form is
 *  `OMNISIGHT_GITHUB_TOKEN_MAP` / `OMNISIGHT_GITLAB_TOKEN_MAP`). The
 *  dedicated masked `/system/settings/git/token-map` endpoint lands in
 *  row 217 — until then, TEST short-circuits to a stub "not wired"
 *  probe. */
interface TokenMapInstance {
  id: string
  platform: "github" | "gitlab"
  host: string
  token: string
  testStatus?: "idle" | "testing" | "ok" | "error"
  testMessage?: string
}

// Serialize instances → JSON map shape consumed by
// `settings.github_token_map` / `settings.gitlab_token_map`
// (env-var name: OMNISIGHT_GITHUB_TOKEN_MAP / OMNISIGHT_GITLAB_TOKEN_MAP).
// Empty list → empty string so the backend treats it as "no map
// configured" rather than `"{}"` (both parse to {} in _load_json_map
// but empty string is the idiomatic "unset" value).
function serializeTokenMap(
  instances: TokenMapInstance[],
  platform: "github" | "gitlab",
): string {
  const entries = instances
    .filter(i => i.platform === platform)
    .map(i => [i.host, i.token] as const)
  if (entries.length === 0) return ""
  return JSON.stringify(Object.fromEntries(entries))
}

function MultipleInstancesSection({
  setVal,
}: {
  setVal: (configKey: string, value: string | boolean) => void
}) {
  const [expanded, setExpanded] = useState(false)
  const [addingGithub, setAddingGithub] = useState(false)
  const [ghHost, setGhHost] = useState("")
  const [ghToken, setGhToken] = useState("")
  const [addingGitlab, setAddingGitlab] = useState(false)
  const [glUrl, setGlUrl] = useState("")
  const [glToken, setGlToken] = useState("")
  const [instances, setInstances] = useState<TokenMapInstance[]>([])

  const resetGithubForm = () => {
    setAddingGithub(false)
    setGhHost("")
    setGhToken("")
  }

  const resetGitlabForm = () => {
    setAddingGitlab(false)
    setGlUrl("")
    setGlToken("")
  }

  // Row 216: every mutation recomputes the two JSON maps and pushes them
  // into the parent's `dirty` reducer via `setVal`, so that the SAVE &
  // APPLY button serialises the instance list into `github_token_map` /
  // `gitlab_token_map`. We compute from the *next* state (inside the
  // setter's updater) so we don't double-render or race React 18 batching.
  const pushTokenMapsFromNext = (next: TokenMapInstance[]) => {
    setVal("github_token_map", serializeTokenMap(next, "github"))
    setVal("gitlab_token_map", serializeTokenMap(next, "gitlab"))
  }

  const handleAddGithub = () => {
    if (!ghHost || !ghToken) return
    const id = `gh-${ghHost}-${Date.now()}`
    setInstances(prev => {
      const next = [...prev, { id, platform: "github" as const, host: ghHost, token: ghToken, testStatus: "idle" as const }]
      pushTokenMapsFromNext(next)
      return next
    })
    resetGithubForm()
  }

  const handleAddGitlab = () => {
    if (!glUrl || !glToken) return
    const id = `gl-${glUrl}-${Date.now()}`
    setInstances(prev => {
      const next = [...prev, { id, platform: "gitlab" as const, host: glUrl, token: glToken, testStatus: "idle" as const }]
      pushTokenMapsFromNext(next)
      return next
    })
    resetGitlabForm()
  }

  const handleRemove = (id: string) => {
    setInstances(prev => {
      const next = prev.filter(i => i.id !== id)
      pushTokenMapsFromNext(next)
      return next
    })
  }

  // Row 215 delivers the button wiring; row 217's backend probe is not
  // reachable yet, so TEST flips into a deterministic stub result so the
  // result-surface codepath is exercised end-to-end at build time.
  const handleTest = async (id: string) => {
    setInstances(prev => prev.map(i =>
      i.id === id ? { ...i, testStatus: "testing", testMessage: undefined } : i
    ))
    await new Promise(r => setTimeout(r, 400))
    setInstances(prev => prev.map(i =>
      i.id === id
        ? { ...i, testStatus: "error", testMessage: "probe endpoint lands in row 217" }
        : i
    ))
  }

  const renderStatusBadge = (inst: TokenMapInstance) => {
    if (inst.testStatus === "testing") {
      return (
        <span className="inline-flex items-center gap-0.5 font-mono text-[8px] text-[var(--neural-blue)]">
          <Loader size={9} className="animate-spin" /> TESTING
        </span>
      )
    }
    if (inst.testStatus === "ok") {
      return (
        <span className="inline-flex items-center gap-0.5 font-mono text-[8px] text-[var(--validation-emerald)]">
          <Check size={9} /> OK
        </span>
      )
    }
    if (inst.testStatus === "error") {
      return (
        <span
          title={inst.testMessage}
          className="inline-flex items-center gap-0.5 font-mono text-[8px] text-[var(--critical-red)]"
        >
          <AlertTriangle size={9} /> ERR
        </span>
      )
    }
    return null
  }

  return (
    <div className="pt-2 border-t border-[var(--border)]/50 mt-1">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-2 px-1 py-1 rounded hover:bg-[var(--background)] transition-colors"
      >
        <span className="font-mono text-[8px] text-[var(--muted-foreground)] uppercase tracking-wider flex-1 text-left">
          Multiple Instances
          {instances.length > 0 && (
            <span className="ml-1 px-1 py-0.5 rounded bg-[var(--neural-blue)]/15 text-[var(--neural-blue)] normal-case">
              {instances.length}
            </span>
          )}
        </span>
        <span className="font-mono text-[8px] text-[var(--muted-foreground)] opacity-60">
          GitHub Enterprise · self-hosted GitLab
        </span>
        {expanded
          ? <ChevronUp size={10} className="text-[var(--muted-foreground)]" />
          : <ChevronDown size={10} className="text-[var(--muted-foreground)]" />}
      </button>
      {expanded && (
        <div className="mt-1 px-1 py-1.5 space-y-1">
          {instances.length === 0 ? (
            <div className="font-mono text-[9px] text-[var(--muted-foreground)] opacity-60">
              No additional instances configured.
            </div>
          ) : (
            <div className="space-y-1">
              {instances.map(inst => {
                const platformColor = inst.platform === "github"
                  ? "var(--neural-blue)"
                  : "var(--hardware-orange)"
                return (
                  <div
                    key={inst.id}
                    className="flex items-center gap-2 p-1.5 rounded border border-[var(--border)] bg-[var(--background)]"
                  >
                    <span
                      className="font-mono text-[8px] px-1 py-0.5 rounded uppercase shrink-0"
                      style={{ backgroundColor: `${platformColor}22`, color: platformColor }}
                    >
                      {inst.platform}
                    </span>
                    <span className="font-mono text-[10px] text-[var(--foreground)] flex-1 truncate" title={inst.host}>
                      {inst.host}
                    </span>
                    <span className="font-mono text-[9px] text-[var(--muted-foreground)] shrink-0" title="Token is masked — full value held only in local state">
                      •••{inst.token.slice(-4)}
                    </span>
                    {renderStatusBadge(inst)}
                    <button
                      onClick={() => handleTest(inst.id)}
                      disabled={inst.testStatus === "testing"}
                      title="Probe this instance's API (row 217 delivers the backend)"
                      className="px-1.5 py-0.5 rounded font-mono text-[8px] bg-[var(--neural-blue)]/10 text-[var(--neural-blue)] hover:bg-[var(--neural-blue)]/20 disabled:opacity-30 transition-colors shrink-0"
                    >
                      TEST
                    </button>
                    <button
                      onClick={() => handleRemove(inst.id)}
                      title="Remove this instance from the map"
                      className="p-0.5 rounded hover:bg-[var(--critical-red)]/10 transition-colors shrink-0"
                    >
                      <Trash2 size={10} className="text-[var(--muted-foreground)] hover:text-[var(--critical-red)]" />
                    </button>
                  </div>
                )
              })}
            </div>
          )}
          <div className="font-mono text-[8px] text-[var(--muted-foreground)] opacity-50 leading-relaxed">
            Map per-host tokens via OMNISIGHT_GITHUB_TOKEN_MAP /
            OMNISIGHT_GITLAB_TOKEN_MAP. SAVE & APPLY serialises this list
            into JSON; masked read-back lands in row 217.
          </div>

          {addingGithub ? (
            <div className="mt-2 p-2 rounded border border-[var(--neural-blue)]/30 bg-[var(--secondary)] space-y-1.5">
              <div className="font-mono text-[8px] text-[var(--neural-blue)] uppercase tracking-wider">
                Add GitHub Instance
              </div>
              <SettingField
                label="Hostname"
                value={ghHost}
                onChange={setGhHost}
              />
              <SettingField
                label="Token"
                value={ghToken}
                type="password"
                onChange={setGhToken}
              />
              <div className="font-mono text-[8px] text-[var(--muted-foreground)] opacity-60 leading-relaxed">
                e.g. github.enterprise.com — used as the key in
                OMNISIGHT_GITHUB_TOKEN_MAP. SAVE & APPLY serialises the list.
              </div>
              <div className="flex gap-2 justify-end">
                <button
                  onClick={resetGithubForm}
                  className="px-2 py-0.5 rounded font-mono text-[9px] text-[var(--muted-foreground)] hover:bg-[var(--background)]"
                >
                  CANCEL
                </button>
                <button
                  disabled={!ghHost || !ghToken}
                  onClick={handleAddGithub}
                  className="px-2 py-0.5 rounded font-mono text-[9px] bg-[var(--neural-blue)] text-black font-semibold disabled:opacity-30"
                  title="Adds this host→token pair to the pending github_token_map JSON; SAVE & APPLY persists it"
                >
                  ADD
                </button>
              </div>
            </div>
          ) : (
            <button
              onClick={() => setAddingGithub(true)}
              className="mt-1 flex items-center gap-1 px-2 py-1 rounded font-mono text-[9px] text-[var(--neural-blue)] hover:bg-[var(--neural-blue)]/10 transition-colors"
            >
              <Plus size={10} /> Add GitHub Instance
            </button>
          )}

          {addingGitlab ? (
            <div className="mt-2 p-2 rounded border border-[var(--hardware-orange)]/30 bg-[var(--secondary)] space-y-1.5">
              <div className="font-mono text-[8px] text-[var(--hardware-orange)] uppercase tracking-wider">
                Add GitLab Instance
              </div>
              <SettingField
                label="URL"
                value={glUrl}
                onChange={setGlUrl}
              />
              <SettingField
                label="Token"
                value={glToken}
                type="password"
                onChange={setGlToken}
              />
              <div className="font-mono text-[8px] text-[var(--muted-foreground)] opacity-60 leading-relaxed">
                e.g. https://gitlab.example.com — used as the key in
                OMNISIGHT_GITLAB_TOKEN_MAP. SAVE & APPLY serialises the list.
              </div>
              <div className="flex gap-2 justify-end">
                <button
                  onClick={resetGitlabForm}
                  className="px-2 py-0.5 rounded font-mono text-[9px] text-[var(--muted-foreground)] hover:bg-[var(--background)]"
                >
                  CANCEL
                </button>
                <button
                  disabled={!glUrl || !glToken}
                  onClick={handleAddGitlab}
                  className="px-2 py-0.5 rounded font-mono text-[9px] bg-[var(--hardware-orange)] text-black font-semibold disabled:opacity-30"
                  title="Adds this URL→token pair to the pending gitlab_token_map JSON; SAVE & APPLY persists it"
                >
                  ADD
                </button>
              </div>
            </div>
          ) : (
            <button
              onClick={() => setAddingGitlab(true)}
              className="mt-1 flex items-center gap-1 px-2 py-1 rounded font-mono text-[9px] text-[var(--hardware-orange)] hover:bg-[var(--hardware-orange)]/10 transition-colors"
            >
              <Plus size={10} /> Add GitLab Instance
            </button>
          )}
        </div>
      )}
    </div>
  )
}

function formatBytes(n: number): string {
  if (n === 0) return "0 B"
  const units = ["B", "KiB", "MiB", "GiB", "TiB"]
  const exp = Math.min(Math.floor(Math.log(n) / Math.log(1024)), units.length - 1)
  const v = n / Math.pow(1024, exp)
  return `${v >= 10 ? v.toFixed(1) : v.toFixed(2)} ${units[exp]}`
}

function StorageQuotaSection() {
  const [usage, setUsage] = useState<api.TenantStorageUsage | null>(null)
  const [cleaning, setCleaning] = useState(false)
  const [lastSummary, setLastSummary] = useState<api.TenantStorageCleanupSummary | null>(null)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      setError(null)
      const u = await api.getStorageUsage()
      setUsage(u)
    } catch (e) {
      setError(String(e))
    }
  }, [])

  useEffect(() => { refresh() }, [refresh])

  const handleCleanup = useCallback(async () => {
    if (!usage) return
    setCleaning(true)
    setError(null)
    try {
      const s = await api.triggerStorageCleanup(usage.tenant_id)
      setLastSummary(s)
      await refresh()
    } catch (e) {
      setError(String(e))
    } finally {
      setCleaning(false)
    }
  }, [usage, refresh])

  if (!usage) {
    return (
      <SettingsSection title="STORAGE QUOTA">
        <div className="font-mono text-[9px] text-[var(--muted-foreground)] py-1 opacity-60">
          {error ? `Failed: ${error}` : "Loading…"}
        </div>
      </SettingsSection>
    )
  }

  const pctSoft = Math.min(100, (usage.usage.total_bytes / usage.quota.soft_bytes) * 100)
  const pctHard = Math.min(100, (usage.usage.total_bytes / usage.quota.hard_bytes) * 100)
  const barColor =
    usage.over_hard ? "var(--critical-red)" :
    usage.over_soft ? "var(--hardware-orange)" :
    "var(--validation-emerald)"

  return (
    <SettingsSection title={`STORAGE QUOTA — ${usage.tenant_id} (${usage.plan})`}>
      <div className="space-y-1.5">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-1.5">
            <HardDrive size={10} className="text-[var(--neural-blue)]" />
            <span className="font-mono text-[10px] text-[var(--foreground)]">
              {formatBytes(usage.usage.total_bytes)}
            </span>
            <span className="font-mono text-[9px] text-[var(--muted-foreground)]">
              / {formatBytes(usage.quota.soft_bytes)} soft / {formatBytes(usage.quota.hard_bytes)} hard
            </span>
          </div>
          {usage.over_hard ? (
            <span className="font-mono text-[8px] px-1.5 py-0.5 rounded bg-[var(--critical-red)]/15 text-[var(--critical-red)] uppercase">
              hard breach
            </span>
          ) : usage.over_soft ? (
            <span className="font-mono text-[8px] px-1.5 py-0.5 rounded bg-[var(--hardware-orange)]/15 text-[var(--hardware-orange)] uppercase">
              over soft
            </span>
          ) : (
            <span className="font-mono text-[8px] px-1.5 py-0.5 rounded bg-[var(--validation-emerald)]/15 text-[var(--validation-emerald)] uppercase">
              healthy
            </span>
          )}
        </div>

        {/* Stacked bars: soft + hard */}
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <span className="font-mono text-[8px] text-[var(--muted-foreground)] w-10 shrink-0">soft</span>
            <div className="flex-1 h-1.5 rounded bg-[var(--secondary)] overflow-hidden">
              <div
                className="h-full transition-all"
                style={{ width: `${pctSoft}%`, backgroundColor: barColor }}
              />
            </div>
            <span className="font-mono text-[8px] text-[var(--muted-foreground)] w-10 shrink-0 text-right">
              {pctSoft.toFixed(0)}%
            </span>
          </div>
          <div className="flex items-center gap-2">
            <span className="font-mono text-[8px] text-[var(--muted-foreground)] w-10 shrink-0">hard</span>
            <div className="flex-1 h-1.5 rounded bg-[var(--secondary)] overflow-hidden">
              <div
                className="h-full transition-all"
                style={{ width: `${pctHard}%`, backgroundColor: barColor }}
              />
            </div>
            <span className="font-mono text-[8px] text-[var(--muted-foreground)] w-10 shrink-0 text-right">
              {pctHard.toFixed(0)}%
            </span>
          </div>
        </div>

        {/* Breakdown */}
        <div className="grid grid-cols-2 gap-x-3 gap-y-0.5 pt-1">
          {[
            ["artifacts", usage.usage.artifacts_bytes],
            ["workflow_runs", usage.usage.workflow_runs_bytes],
            ["backups", usage.usage.backups_bytes],
            ["ingest_tmp", usage.usage.ingest_tmp_bytes],
          ].map(([label, bytes]) => (
            <div key={String(label)} className="flex items-center justify-between">
              <span className="font-mono text-[9px] text-[var(--muted-foreground)]">{label}</span>
              <span className="font-mono text-[9px] text-[var(--foreground)]">{formatBytes(Number(bytes))}</span>
            </div>
          ))}
        </div>

        <div className="flex items-center gap-2 justify-end pt-1">
          {lastSummary && (
            <span className="font-mono text-[8px] text-[var(--muted-foreground)] mr-auto">
              freed {formatBytes(lastSummary.usage_before_bytes - lastSummary.usage_after_bytes)} ({lastSummary.deleted.length} run{lastSummary.deleted.length === 1 ? "" : "s"})
              {lastSummary.skipped_keep.length > 0 ? `, kept ${lastSummary.skipped_keep.length}` : ""}
            </span>
          )}
          <button
            onClick={refresh}
            disabled={cleaning}
            title="Refresh usage"
            className="p-1 rounded text-[var(--muted-foreground)] hover:bg-[var(--neural-blue)]/10 hover:text-[var(--neural-blue)] disabled:opacity-30 transition-colors"
          >
            <RefreshCw size={10} className={cleaning ? "animate-spin" : ""} />
          </button>
          <button
            onClick={handleCleanup}
            disabled={cleaning}
            className="px-2 py-0.5 rounded font-mono text-[9px] bg-[var(--neural-blue)]/15 text-[var(--neural-blue)] hover:bg-[var(--neural-blue)]/25 disabled:opacity-30 transition-colors"
          >
            {cleaning ? "CLEANING…" : "RUN LRU CLEANUP"}
          </button>
        </div>

        {error && (
          <div className="font-mono text-[9px] text-[var(--critical-red)] pt-1">
            {error}
          </div>
        )}
      </div>
    </SettingsSection>
  )
}

function CircuitBreakerSection() {
  const [data, setData] = useState<api.CircuitBreakerResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [resetting, setResetting] = useState(false)

  const refresh = useCallback(async () => {
    try {
      setError(null)
      const r = await api.getCircuitBreakers("tenant")
      setData(r)
    } catch (e) {
      setError(String(e))
    }
  }, [])

  useEffect(() => {
    refresh()
    // M3: cooldown ticks every second; refresh every 10s so stale state
    // doesn't linger after a key recovers without manual reload.
    const id = setInterval(refresh, 10_000)
    return () => clearInterval(id)
  }, [refresh])

  const handleReset = useCallback(async (provider?: string, fingerprint?: string) => {
    setResetting(true)
    try {
      await api.resetCircuitBreaker({ provider, fingerprint })
      await refresh()
    } catch (e) {
      setError(String(e))
    } finally {
      setResetting(false)
    }
  }, [refresh])

  return (
    <div className="pt-2 border-t border-[var(--border)]/50 mt-1">
      <div className="flex items-center justify-between pb-1">
        <span className="font-mono text-[8px] text-[var(--muted-foreground)] uppercase tracking-wider">
          Circuit Breakers — {data?.tenant_id ?? "?"}
        </span>
        <div className="flex items-center gap-1">
          {data && data.circuits.length > 0 && (
            <button
              onClick={() => handleReset()}
              disabled={resetting}
              className="px-1.5 py-0.5 rounded font-mono text-[8px] bg-[var(--secondary)] text-[var(--muted-foreground)] hover:bg-[var(--neural-blue)]/15 hover:text-[var(--neural-blue)] disabled:opacity-30 transition-colors"
              title="Reset all circuits for this tenant"
            >
              RESET ALL
            </button>
          )}
          <button
            onClick={refresh}
            disabled={resetting}
            title="Refresh circuit state"
            className="p-0.5 rounded text-[var(--muted-foreground)] hover:bg-[var(--neural-blue)]/10 hover:text-[var(--neural-blue)] disabled:opacity-30 transition-colors"
          >
            <RefreshCw size={9} className={resetting ? "animate-spin" : ""} />
          </button>
        </div>
      </div>

      {error && (
        <div className="font-mono text-[9px] text-[var(--critical-red)] py-1">{error}</div>
      )}

      {!data ? (
        <div className="font-mono text-[9px] text-[var(--muted-foreground)] opacity-60 py-1">
          Loading…
        </div>
      ) : data.circuits.length === 0 ? (
        <div className="font-mono text-[9px] text-[var(--muted-foreground)] opacity-60 py-1">
          All circuits healthy — no failures recorded for this tenant.
        </div>
      ) : (
        <div className="space-y-1">
          {data.circuits.map((c) => {
            const open = c.open
            const colorVar = open ? "var(--critical-red)" : "var(--validation-emerald)"
            return (
              <div
                key={`${c.provider}/${c.fingerprint}`}
                className="flex items-center gap-2 p-1.5 rounded border border-[var(--border)] bg-[var(--background)]"
              >
                <span
                  className="w-1.5 h-1.5 rounded-full shrink-0"
                  style={{ backgroundColor: colorVar }}
                />
                <span className="font-mono text-[10px] text-[var(--foreground)] w-16 shrink-0 truncate">
                  {c.provider}
                </span>
                <span className="font-mono text-[9px] text-[var(--muted-foreground)] flex-1 truncate">
                  {c.fingerprint}
                </span>
                <span
                  className="font-mono text-[8px] px-1.5 py-0.5 rounded uppercase shrink-0"
                  style={{ backgroundColor: `${colorVar}20`, color: colorVar }}
                >
                  {open ? `OPEN ${c.cooldown_remaining}s` : "CLOSED"}
                </span>
                <span className="font-mono text-[8px] text-[var(--muted-foreground)] shrink-0">
                  {c.failure_count} fail{c.failure_count === 1 ? "" : "s"}
                </span>
                {open && (
                  <button
                    onClick={() => handleReset(c.provider, c.fingerprint)}
                    disabled={resetting}
                    title={c.reason ?? "Reset this circuit"}
                    className="px-1 py-0.5 rounded font-mono text-[8px] bg-[var(--neural-blue)]/15 text-[var(--neural-blue)] hover:bg-[var(--neural-blue)]/25 disabled:opacity-30 transition-colors shrink-0"
                  >
                    RESET
                  </button>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  M6 — Network Egress (per-tenant allow-list + request flow)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

function NetworkEgressSection() {
  const [policy, setPolicy] = useState<api.TenantEgressPolicy | null>(null)
  const [requests, setRequests] = useState<api.TenantEgressRequest[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [draftKind, setDraftKind] = useState<"host" | "cidr">("host")
  const [draftValue, setDraftValue] = useState("")
  const [draftJustify, setDraftJustify] = useState("")

  const refresh = useCallback(async () => {
    try {
      setError(null)
      const [p, r] = await Promise.all([
        api.getMyEgressPolicy(),
        api.listMyEgressRequests(),
      ])
      setPolicy(p.policy)
      setRequests(r.requests)
    } catch (e) {
      setError(String(e))
    }
  }, [])

  useEffect(() => { refresh() }, [refresh])

  const handleSubmit = useCallback(async () => {
    if (!draftValue.trim()) return
    setBusy(true)
    setError(null)
    try {
      await api.submitEgressRequest({
        kind: draftKind,
        value: draftValue.trim(),
        justification: draftJustify.trim(),
      })
      setDraftValue("")
      setDraftJustify("")
      await refresh()
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }, [draftKind, draftValue, draftJustify, refresh])

  const handleApprove = useCallback(async (rid: string) => {
    setBusy(true)
    setError(null)
    try {
      await api.approveEgressRequest(rid)
      await refresh()
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }, [refresh])

  const handleReject = useCallback(async (rid: string) => {
    setBusy(true)
    setError(null)
    try {
      await api.rejectEgressRequest(rid)
      await refresh()
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }, [refresh])

  if (!policy) {
    return (
      <SettingsSection title="NETWORK EGRESS">
        <div className="font-mono text-[9px] text-[var(--muted-foreground)] py-1 opacity-60">
          {error ? `Failed: ${error}` : "Loading…"}
        </div>
      </SettingsSection>
    )
  }

  const pending = requests.filter(r => r.status === "pending")
  const recent = requests.filter(r => r.status !== "pending").slice(0, 5)

  return (
    <SettingsSection title={`NETWORK EGRESS — ${policy.tenant_id} (default: ${policy.default_action})`}>
      <div className="space-y-2">
        {/* Allow-list summary */}
        <div className="grid grid-cols-2 gap-2">
          <div>
            <div className="font-mono text-[9px] text-[var(--muted-foreground)] mb-0.5">
              ALLOWED HOSTS ({policy.allowed_hosts.length})
            </div>
            <div className="font-mono text-[9px] text-[var(--foreground)] space-y-0.5 max-h-24 overflow-auto">
              {policy.allowed_hosts.length === 0 ? (
                <span className="opacity-50">— none —</span>
              ) : policy.allowed_hosts.map(h => (
                <div key={h}>{h}</div>
              ))}
            </div>
          </div>
          <div>
            <div className="font-mono text-[9px] text-[var(--muted-foreground)] mb-0.5">
              ALLOWED CIDRS ({policy.allowed_cidrs.length})
            </div>
            <div className="font-mono text-[9px] text-[var(--foreground)] space-y-0.5 max-h-24 overflow-auto">
              {policy.allowed_cidrs.length === 0 ? (
                <span className="opacity-50">— none —</span>
              ) : policy.allowed_cidrs.map(c => (
                <div key={c}>{c}</div>
              ))}
            </div>
          </div>
        </div>

        {/* Submit a request */}
        <div className="border-t border-[var(--border)] pt-2 space-y-1">
          <div className="font-mono text-[9px] text-[var(--muted-foreground)]">REQUEST AN ADDITION</div>
          <div className="flex items-center gap-1.5">
            <select
              value={draftKind}
              onChange={e => setDraftKind(e.target.value as "host" | "cidr")}
              className="font-mono text-[10px] bg-[var(--secondary)] border border-[var(--border)] rounded px-1.5 py-0.5"
            >
              <option value="host">host</option>
              <option value="cidr">cidr</option>
            </select>
            <input
              type="text"
              value={draftValue}
              onChange={e => setDraftValue(e.target.value)}
              placeholder={draftKind === "host" ? "api.openai.com[:443]" : "10.0.0.0/8"}
              className="flex-1 font-mono text-[10px] bg-[var(--secondary)] border border-[var(--border)] rounded px-1.5 py-0.5"
            />
            <button
              onClick={handleSubmit}
              disabled={busy || !draftValue.trim()}
              className="px-2 py-0.5 rounded font-mono text-[9px] bg-[var(--neural-blue)]/15 text-[var(--neural-blue)] hover:bg-[var(--neural-blue)]/25 disabled:opacity-30 transition-colors"
            >
              <Plus size={10} className="inline -mt-0.5 mr-0.5" /> SUBMIT
            </button>
          </div>
          <input
            type="text"
            value={draftJustify}
            onChange={e => setDraftJustify(e.target.value)}
            placeholder="(optional) justification — why does the agent need this?"
            className="w-full font-mono text-[10px] bg-[var(--secondary)] border border-[var(--border)] rounded px-1.5 py-0.5"
          />
        </div>

        {/* Pending requests */}
        {pending.length > 0 && (
          <div className="border-t border-[var(--border)] pt-2 space-y-0.5">
            <div className="font-mono text-[9px] text-[var(--muted-foreground)]">
              PENDING ({pending.length}) — admin can approve/reject
            </div>
            {pending.map(r => (
              <div
                key={r.id}
                className="flex items-center gap-2 font-mono text-[9px] py-0.5"
              >
                <span className="px-1 py-0.5 rounded bg-[var(--hardware-orange)]/15 text-[var(--hardware-orange)] uppercase">
                  {r.kind}
                </span>
                <span className="flex-1 truncate text-[var(--foreground)]">{r.value}</span>
                <span className="text-[var(--muted-foreground)] truncate max-w-[120px]" title={r.requested_by}>
                  {r.requested_by.replace(/^user:/, "")}
                </span>
                <button
                  onClick={() => handleApprove(r.id)}
                  disabled={busy}
                  className="px-1.5 py-0 rounded text-[9px] bg-[var(--validation-emerald)]/15 text-[var(--validation-emerald)] hover:bg-[var(--validation-emerald)]/25 disabled:opacity-30"
                >
                  approve
                </button>
                <button
                  onClick={() => handleReject(r.id)}
                  disabled={busy}
                  className="px-1.5 py-0 rounded text-[9px] bg-[var(--critical-red)]/15 text-[var(--critical-red)] hover:bg-[var(--critical-red)]/25 disabled:opacity-30"
                >
                  reject
                </button>
              </div>
            ))}
          </div>
        )}

        {/* Decided history */}
        {recent.length > 0 && (
          <div className="border-t border-[var(--border)] pt-2 space-y-0.5">
            <div className="font-mono text-[9px] text-[var(--muted-foreground)]">RECENT DECISIONS</div>
            {recent.map(r => (
              <div
                key={r.id}
                className="flex items-center gap-2 font-mono text-[9px] py-0.5 opacity-80"
              >
                <span
                  className={`px-1 py-0.5 rounded uppercase ${
                    r.status === "approved"
                      ? "bg-[var(--validation-emerald)]/15 text-[var(--validation-emerald)]"
                      : "bg-[var(--critical-red)]/15 text-[var(--critical-red)]"
                  }`}
                >
                  {r.status}
                </span>
                <span className="flex-1 truncate">{r.kind}: {r.value}</span>
                <span className="text-[var(--muted-foreground)] truncate max-w-[120px]" title={r.decided_by ?? ""}>
                  by {(r.decided_by ?? "").replace(/^user:/, "") || "—"}
                </span>
              </div>
            ))}
          </div>
        )}

        {error && (
          <div className="font-mono text-[9px] text-[var(--critical-red)]">{error}</div>
        )}

        <div className="font-mono text-[8px] text-[var(--muted-foreground)] opacity-60">
          {policy.allowed_hosts.length === 0 && policy.allowed_cidrs.length === 0 && policy.default_action === "deny"
            ? "Default-deny in effect — sandboxes for this tenant are air-gapped (--network none)."
            : "iptables installer reads this policy via `python -m backend.tenant_egress emit-rules`."}
        </div>
      </div>
    </SettingsSection>
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
            {/* M3: per-tenant per-provider per-key circuit breaker state */}
            <CircuitBreakerSection />
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
            {/* B14 Part B rows 1-5: collapsible Multiple Instances sub-area
                — child pipes JSON token-maps into parent `dirty` on each
                mutation so SAVE & APPLY persists OMNISIGHT_*_TOKEN_MAP. */}
            <MultipleInstancesSection setVal={setVal} />
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

          {/* I4: Tenant-scoped secrets */}
          <TenantSecretsSection settingsData={settingsData} />

          {/* M2: Per-tenant disk quota + LRU cleanup */}
          <StorageQuotaSection />

          {/* M6: Per-tenant egress allow-list + approval workflow */}
          <NetworkEgressSection />

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
