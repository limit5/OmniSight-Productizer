"use client"

import { useState, useEffect, useCallback } from "react"
import { createPortal } from "react-dom"
import { Settings, X, Check, AlertTriangle, Loader, ChevronDown, ChevronUp, WifiOff, Key, Plus, Trash2, HardDrive, RefreshCw, Copy, Users, ShieldCheck, Webhook } from "lucide-react"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
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

/**
 * B14 Part C rows 221–224:
 *
 * Modal that walks the operator through the 5-step Gerrit Code Review
 * setup. The entry-point shell + 5-step overview landed in row 221;
 * rows 222–225 wired Steps 1–4 (Test Connection, SSH key, merger-agent-bot,
 * submit-rule). Row 226 wires Step 5 — the inbound webhook URL + HMAC
 * secret the operator pastes into Gerrit's `webhooks.config`.
 *
 * Step 1 collects the Gerrit REST URL (optional), SSH host, and SSH
 * port, then calls `api.testGitForgeToken({ provider: "gerrit", ssh_host,
 * ssh_port, url })` — the same non-mutating backend probe the Bootstrap
 * wizard's Step 3.5 Gerrit tab uses — so both entry-points share one
 * code path (`_probe_gerrit_ssh` → `ssh -p {port} {host} gerrit version`).
 * On success the Gerrit version surfaces inline and Step 1's badge flips
 * PENDING → DONE. Step 2 loads the OmniSight SSH public key via
 * `api.getGitForgeSshPubkey()` and guides the operator through pasting
 * it into Gerrit Settings → SSH Keys. Step 3 shows the `gerrit
 * create-group` + `gerrit set-members` SSH commands the operator must
 * run as a Gerrit admin, then calls `api.verifyGerritMergerBot(...)` to
 * confirm the `merger-agent-bot` group exists and has ≥1 member — the
 * AI half of the O7 dual-+2 submit gate. Step 4 fetches
 * `refs/meta/config:project.config` and pattern-matches the dual-+2 ACL.
 * Step 5 surfaces the inbound webhook URL (derived from the inbound
 * Request's Forwarded headers / base_url) and the HMAC-SHA256 secret
 * status (configured / not). Operators can mint + persist a fresh
 * `gerrit_webhook_secret` via `api.generateGerritWebhookSecret()` — the
 * plain value is shown exactly once for copy-to-clipboard; subsequent
 * reads return only the masked preview. Rotate to recover.
 *
 * Steps 1–4 do not persist to `settings.gerrit_*`. Step 5 mutates one
 * field (`settings.gerrit_webhook_secret`) via the rotate endpoint —
 * that's intentional because the wizard is the only place this secret
 * is generated. Row 227 adds the Finalize pane (gated on Step 5 ack):
 * a single atomic write into `settings.gerrit_url`, `gerrit_ssh_host`,
 * `gerrit_ssh_port`, `gerrit_project`, `gerrit_replication_targets`,
 * and `gerrit_enabled = true`. The success path renders the
 * 「Gerrit 整合已啟用」banner so the operator knows the wizard's
 * collected values are now load-bearing.
 *
 * Rendered via createPortal at z-[110] to sit above the parent IntegrationSettings
 * portal (z-[100]); backdrop click and X close.
 */
const GERRIT_DEFAULT_SSH_PORT = 29418

function GerritSetupWizardDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [url, setUrl] = useState("")
  const [sshHost, setSshHost] = useState("")
  const [sshPort, setSshPort] = useState<string>(String(GERRIT_DEFAULT_SSH_PORT))
  const [testing, setTesting] = useState(false)
  const [result, setResult] = useState<api.GitForgeTokenTestResult | null>(null)
  // B14 Part C row 223 — Step 2: fetch + display the OmniSight SSH public
  // key so the operator can paste it into Gerrit Settings → SSH Keys.
  const [pubkeyLoading, setPubkeyLoading] = useState(false)
  const [pubkey, setPubkey] = useState<api.GitForgeSshPubkey | null>(null)
  const [pubkeyCopied, setPubkeyCopied] = useState(false)
  const [step2Ackd, setStep2Ackd] = useState(false)
  // B14 Part C row 224 — Step 3: verify that `merger-agent-bot` exists as
  // a Gerrit group with at least one member (the AI half of the O7
  // dual-+2 submit gate). Group creation + membership remain manual per
  // the runbook — we only probe via `gerrit ls-members`.
  const [botVerifying, setBotVerifying] = useState(false)
  const [botVerify, setBotVerify] = useState<api.GerritBotVerifyResult | null>(null)
  const [botCmdCopied, setBotCmdCopied] = useState(false)
  const [step3Ackd, setStep3Ackd] = useState(false)
  // B14 Part C row 225 — Step 4: verify that the target project's
  // `project.config` on `refs/meta/config` carries the O7 dual-+2 ACL
  // (ai-reviewer-bots + non-ai-reviewer label grants, submit gated to
  // non-ai-reviewer). The probe never mutates Gerrit — rule installation
  // stays manual per docs/ops/gerrit_dual_two_rule.md §2.
  const [submitRuleProject, setSubmitRuleProject] = useState("")
  const [submitRuleVerifying, setSubmitRuleVerifying] = useState(false)
  const [submitRuleVerify, setSubmitRuleVerify] =
    useState<api.GerritSubmitRuleVerifyResult | null>(null)
  const [step4Ackd, setStep4Ackd] = useState(false)
  // B14 Part C row 226 — Step 5: webhook 設定引導. Show the inbound webhook
  // URL the operator must paste into Gerrit's `webhooks.config` and the
  // status of the HMAC-SHA256 secret. Generating mints + persists a fresh
  // secret on the backend and returns the plain value exactly once — we
  // hold it in component state so the operator can copy it before closing.
  const [webhookInfo, setWebhookInfo] = useState<api.GerritWebhookInfo | null>(null)
  const [webhookInfoLoading, setWebhookInfoLoading] = useState(false)
  const [webhookGenerating, setWebhookGenerating] = useState(false)
  const [webhookSecretPlain, setWebhookSecretPlain] = useState<string>("")
  const [webhookGenError, setWebhookGenError] = useState<string>("")
  const [webhookUrlCopied, setWebhookUrlCopied] = useState(false)
  const [webhookSecretCopied, setWebhookSecretCopied] = useState(false)
  const [step5Ackd, setStep5Ackd] = useState(false)
  // B14 Part C row 227 — Finalize: write the wizard's collected
  // SSH/REST/project values into `settings.gerrit_*` and flip
  // `gerrit_enabled = true`. Steps 1–5 only mutate
  // `gerrit_webhook_secret` (Step 5 generate); without finalize the
  // operator would have to re-enter every value into the Settings form
  // by hand to actually turn the integration on.
  const [finalizing, setFinalizing] = useState(false)
  const [finalizeResult, setFinalizeResult] =
    useState<api.GerritFinalizeResult | null>(null)
  const [finalizeError, setFinalizeError] = useState<string>("")

  const parsedPort = (() => {
    const trimmed = sshPort.trim()
    if (!trimmed) return GERRIT_DEFAULT_SSH_PORT
    const n = Number(trimmed)
    return Number.isFinite(n) && Number.isInteger(n) ? n : NaN
  })()
  const portValid =
    Number.isInteger(parsedPort) && parsedPort >= 1 && parsedPort <= 65535
  const canTest = sshHost.trim().length > 0 && portValid && !testing

  const onTest = useCallback(async () => {
    if (!canTest) return
    setTesting(true)
    setResult(null)
    try {
      const res = await api.testGitForgeToken({
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
      setTesting(false)
    }
  }, [sshHost, parsedPort, url, canTest])

  const step1Done = result?.status === "ok"

  const onLoadPubkey = useCallback(async () => {
    setPubkeyLoading(true)
    setPubkeyCopied(false)
    try {
      const res = await api.getGitForgeSshPubkey()
      setPubkey(res)
    } catch (err) {
      setPubkey({
        status: "error",
        message:
          err instanceof Error
            ? err.message
            : "Failed to load SSH public key",
      })
    } finally {
      setPubkeyLoading(false)
    }
  }, [])

  const onCopyPubkey = useCallback(async () => {
    const text = pubkey?.public_key
    if (!text) return
    try {
      await navigator.clipboard.writeText(text)
      setPubkeyCopied(true)
    } catch {
      /* clipboard may not be available (e.g. non-secure context) */
    }
  }, [pubkey])

  const onVerifyBot = useCallback(async () => {
    if (!sshHost.trim() || !portValid) return
    setBotVerifying(true)
    setBotVerify(null)
    try {
      const res = await api.verifyGerritMergerBot({
        ssh_host: sshHost.trim(),
        ssh_port: parsedPort as number,
      })
      setBotVerify(res)
    } catch (err) {
      setBotVerify({
        status: "error",
        message:
          err instanceof Error
            ? err.message
            : "Failed to reach the Gerrit ls-members probe",
      })
    } finally {
      setBotVerifying(false)
    }
  }, [sshHost, parsedPort, portValid])

  const onVerifySubmitRule = useCallback(async () => {
    if (!sshHost.trim() || !portValid || !submitRuleProject.trim()) return
    setSubmitRuleVerifying(true)
    setSubmitRuleVerify(null)
    try {
      const res = await api.verifyGerritSubmitRule({
        ssh_host: sshHost.trim(),
        ssh_port: parsedPort as number,
        project: submitRuleProject.trim(),
      })
      setSubmitRuleVerify(res)
    } catch (err) {
      setSubmitRuleVerify({
        status: "error",
        message:
          err instanceof Error
            ? err.message
            : "Failed to reach the Gerrit submit-rule probe",
      })
    } finally {
      setSubmitRuleVerifying(false)
    }
  }, [sshHost, parsedPort, portValid, submitRuleProject])

  const onLoadWebhookInfo = useCallback(async () => {
    setWebhookInfoLoading(true)
    try {
      const res = await api.getGerritWebhookInfo()
      setWebhookInfo(res)
    } catch (err) {
      setWebhookInfo({
        status: "error",
        message:
          err instanceof Error
            ? err.message
            : "Failed to load webhook info",
      })
    } finally {
      setWebhookInfoLoading(false)
    }
  }, [])

  const onGenerateWebhookSecret = useCallback(async () => {
    setWebhookGenerating(true)
    setWebhookGenError("")
    setWebhookSecretCopied(false)
    try {
      const res = await api.generateGerritWebhookSecret()
      if (res.status === "ok" && res.secret) {
        setWebhookSecretPlain(res.secret)
        setWebhookInfo({
          status: "ok",
          webhook_url: res.webhook_url,
          secret_configured: true,
          secret_masked: res.secret_masked,
          signature_header: res.signature_header,
          signature_algorithm: res.signature_algorithm,
        })
      } else {
        setWebhookGenError(res.message || "Failed to generate secret")
      }
    } catch (err) {
      setWebhookGenError(
        err instanceof Error ? err.message : "Failed to generate secret",
      )
    } finally {
      setWebhookGenerating(false)
    }
  }, [])

  const onCopyWebhookUrl = useCallback(async () => {
    const text = webhookInfo?.webhook_url
    if (!text) return
    try {
      await navigator.clipboard.writeText(text)
      setWebhookUrlCopied(true)
    } catch {
      /* clipboard may not be available (e.g. non-secure context) */
    }
  }, [webhookInfo])

  const onCopyWebhookSecret = useCallback(async () => {
    if (!webhookSecretPlain) return
    try {
      await navigator.clipboard.writeText(webhookSecretPlain)
      setWebhookSecretCopied(true)
    } catch {
      /* clipboard may not be available (e.g. non-secure context) */
    }
  }, [webhookSecretPlain])

  const onFinalize = useCallback(async () => {
    if (!sshHost.trim() || !portValid) return
    setFinalizing(true)
    setFinalizeError("")
    setFinalizeResult(null)
    try {
      const res = await api.finalizeGerritIntegration({
        url: url.trim(),
        ssh_host: sshHost.trim(),
        ssh_port: parsedPort as number,
        project: submitRuleProject.trim(),
      })
      if (res.status === "ok" && res.enabled) {
        setFinalizeResult(res)
      } else {
        setFinalizeError(res.message || "Failed to enable Gerrit integration")
      }
    } catch (err) {
      setFinalizeError(
        err instanceof Error ? err.message : "Failed to enable Gerrit integration",
      )
    } finally {
      setFinalizing(false)
    }
  }, [sshHost, parsedPort, portValid, url, submitRuleProject])

  if (!open || typeof document === "undefined") return null

  const pubkeyReady = pubkey?.status === "ok" && !!pubkey.public_key
  const step2Badge: "DONE" | "READY" | "PENDING" = step2Ackd
    ? "DONE"
    : pubkeyReady
      ? "READY"
      : "PENDING"

  const botGroupReady =
    botVerify?.status === "ok" && (botVerify.member_count ?? 0) > 0
  const step3Badge: "DONE" | "READY" | "PENDING" = step3Ackd
    ? "DONE"
    : botGroupReady
      ? "READY"
      : "PENDING"

  const step3Done = step3Ackd
  const submitRuleReady = submitRuleVerify?.status === "ok"
  const step4Badge: "DONE" | "READY" | "PENDING" = step4Ackd
    ? "DONE"
    : submitRuleReady
      ? "READY"
      : "PENDING"
  const submitRuleProjectValid = /^[A-Za-z0-9][A-Za-z0-9_\-./]{0,199}$/.test(
    submitRuleProject.trim(),
  )
  const canVerifySubmitRule =
    step3Done &&
    submitRuleProject.trim().length > 0 &&
    submitRuleProjectValid &&
    !submitRuleVerifying
  const submitRuleChecks = submitRuleVerify?.checks ?? []

  const step4Done = step4Ackd
  const webhookSecretReady =
    !!webhookInfo && webhookInfo.status === "ok" && !!webhookInfo.secret_configured
  const step5Badge: "DONE" | "READY" | "PENDING" = step5Ackd
    ? "DONE"
    : webhookSecretReady
      ? "READY"
      : "PENDING"

  const botHostSlug = sshHost.trim() || "<host>"
  const botPortSlug = portValid ? String(parsedPort) : String(GERRIT_DEFAULT_SSH_PORT)
  const botSetupCommands =
    `# 1) Create the three groups (admin-only; run once per Gerrit instance):\n` +
    `ssh -p ${botPortSlug} ${botHostSlug} gerrit create-group merger-agent-bot \\\n` +
    `    --visible-to-all --description "O6 Merger Agent service account."\n` +
    `ssh -p ${botPortSlug} ${botHostSlug} gerrit create-group ai-reviewer-bots \\\n` +
    `    --visible-to-all --description "Umbrella for every AI reviewer."\n` +
    `ssh -p ${botPortSlug} ${botHostSlug} gerrit create-group non-ai-reviewer \\\n` +
    `    --visible-to-all --description "Humans ONLY. Bots forbidden."\n\n` +
    `# 2) Add the bot service account to both bot groups:\n` +
    `ssh -p ${botPortSlug} ${botHostSlug} gerrit set-members merger-agent-bot \\\n` +
    `    --add merger-agent-bot@svc.omnisight.internal\n` +
    `ssh -p ${botPortSlug} ${botHostSlug} gerrit set-members ai-reviewer-bots \\\n` +
    `    --add merger-agent-bot@svc.omnisight.internal`

  const onCopyBotCommands = async () => {
    try {
      await navigator.clipboard.writeText(botSetupCommands)
      setBotCmdCopied(true)
    } catch {
      /* clipboard may not be available (e.g. non-secure context) */
    }
  }

  return createPortal(
    <div className="fixed inset-0 z-[110] flex items-center justify-center">
      <div className="absolute inset-0 bg-black/70 backdrop-blur-sm" onClick={onClose} />
      <div className="relative z-10 w-full max-w-xl max-h-[85vh] m-4 bg-[var(--card)] border border-[var(--border)] rounded-lg shadow-2xl flex flex-col overflow-hidden">
        <div className="flex items-center justify-between px-4 py-3 border-b border-[var(--border)] bg-[var(--secondary)]">
          <div className="flex items-center gap-2">
            <Settings size={14} className="text-[var(--neural-blue)]" />
            <h2 className="font-sans text-sm font-semibold tracking-fui text-[var(--neural-blue)]">
              GERRIT SETUP WIZARD
            </h2>
          </div>
          <button
            onClick={onClose}
            className="p-1 rounded hover:bg-[var(--background)] transition-colors"
            aria-label="Close wizard"
          >
            <X size={14} className="text-[var(--muted-foreground)]" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto overflow-x-hidden p-4 space-y-3">
          <p className="font-mono text-[10px] leading-relaxed text-[var(--foreground)]">
            引導你把 <strong>Gerrit Code Review</strong> 一步步接上 OmniSight 的 dual-sign merge 流程。共 5 個步驟 + Finalize（寫入 config 並啟用整合）。
          </p>

          <div
            data-testid="gerrit-wizard-step-1"
            className="p-3 rounded border border-[var(--neural-blue)]/40 bg-[var(--background)] space-y-2"
          >
            <div className="flex items-start gap-2">
              <span className="flex-shrink-0 w-5 h-5 rounded-full bg-[var(--neural-blue)]/10 text-[var(--neural-blue)] text-[9px] font-mono flex items-center justify-center font-semibold">
                1
              </span>
              <div className="flex-1 min-w-0">
                <div className="font-mono text-[10px] font-semibold text-[var(--foreground)]">
                  Step 1 — Connection probe
                </div>
                <div className="font-mono text-[9px] text-[var(--muted-foreground)] leading-relaxed mt-0.5">
                  輸入 Gerrit REST URL + SSH host / port，Test Connection 驗證 reachability 與版本。
                </div>
              </div>
              <span
                data-testid="gerrit-wizard-step-1-badge"
                className={`flex-shrink-0 font-mono text-[8px] px-1.5 py-0.5 rounded self-start ${
                  step1Done
                    ? "bg-[var(--validation-emerald)]/20 text-[var(--validation-emerald)]"
                    : "bg-[var(--secondary)] text-[var(--muted-foreground)]"
                }`}
              >
                {step1Done ? "DONE" : "PENDING"}
              </span>
            </div>

            <div className="space-y-1.5 pt-1">
              <div className="flex items-center gap-2">
                <label
                  htmlFor="gerrit-wizard-url"
                  className="font-mono text-[9px] text-[var(--muted-foreground)] w-20 shrink-0"
                >
                  REST URL
                </label>
                <input
                  id="gerrit-wizard-url"
                  data-testid="gerrit-wizard-url"
                  type="text"
                  autoComplete="off"
                  spellCheck={false}
                  value={url}
                  onChange={(e) => {
                    setUrl(e.target.value)
                    setResult(null)
                  }}
                  placeholder="https://gerrit.example.com (optional)"
                  className="flex-1 font-mono text-[10px] px-2 py-1 rounded bg-[var(--background)] border border-[var(--border)] text-[var(--foreground)] focus:outline-none focus:border-[var(--neural-blue)]"
                />
              </div>
              <div className="flex items-center gap-2">
                <label
                  htmlFor="gerrit-wizard-ssh-host"
                  className="font-mono text-[9px] text-[var(--muted-foreground)] w-20 shrink-0"
                >
                  SSH Host
                </label>
                <input
                  id="gerrit-wizard-ssh-host"
                  data-testid="gerrit-wizard-ssh-host"
                  type="text"
                  autoComplete="off"
                  spellCheck={false}
                  value={sshHost}
                  onChange={(e) => {
                    setSshHost(e.target.value)
                    setResult(null)
                  }}
                  placeholder="merger-agent-bot@gerrit.example.com"
                  className="flex-1 font-mono text-[10px] px-2 py-1 rounded bg-[var(--background)] border border-[var(--border)] text-[var(--foreground)] focus:outline-none focus:border-[var(--neural-blue)]"
                />
              </div>
              <div className="flex items-center gap-2">
                <label
                  htmlFor="gerrit-wizard-ssh-port"
                  className="font-mono text-[9px] text-[var(--muted-foreground)] w-20 shrink-0"
                >
                  SSH Port
                </label>
                <input
                  id="gerrit-wizard-ssh-port"
                  data-testid="gerrit-wizard-ssh-port"
                  type="number"
                  min={1}
                  max={65535}
                  step={1}
                  autoComplete="off"
                  value={sshPort}
                  onChange={(e) => {
                    setSshPort(e.target.value)
                    setResult(null)
                  }}
                  placeholder={String(GERRIT_DEFAULT_SSH_PORT)}
                  className="w-28 font-mono text-[10px] px-2 py-1 rounded bg-[var(--background)] border border-[var(--border)] text-[var(--foreground)] focus:outline-none focus:border-[var(--neural-blue)]"
                />
                <span className="font-mono text-[9px] text-[var(--muted-foreground)]">
                  (default {GERRIT_DEFAULT_SSH_PORT})
                </span>
              </div>
              {!portValid && sshPort.trim().length > 0 && (
                <div
                  data-testid="gerrit-wizard-port-invalid"
                  className="font-mono text-[9px] text-[var(--critical-red)]"
                >
                  Port must be an integer between 1 and 65535.
                </div>
              )}
              <div className="flex items-center gap-2 pt-1">
                <button
                  type="button"
                  data-testid="gerrit-wizard-test"
                  onClick={onTest}
                  disabled={!canTest}
                  className="flex items-center gap-1.5 px-2.5 py-1 rounded font-mono text-[10px] bg-[var(--neural-blue)]/10 text-[var(--neural-blue)] hover:bg-[var(--neural-blue)]/20 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                >
                  {testing ? (
                    <Loader size={10} className="animate-spin" />
                  ) : (
                    <Check size={10} />
                  )}
                  {testing ? "Testing…" : "Test Connection"}
                </button>
              </div>
              {result && (
                <div
                  data-testid="gerrit-wizard-result"
                  data-status={result.status}
                  className={`font-mono text-[9px] px-2 py-1 rounded flex items-start gap-1.5 ${
                    result.status === "ok"
                      ? "bg-[var(--validation-emerald)]/10 text-[var(--validation-emerald)]"
                      : "bg-[var(--critical-red)]/10 text-[var(--critical-red)]"
                  }`}
                >
                  {result.status === "ok" ? (
                    <Check size={10} className="mt-0.5 flex-shrink-0" />
                  ) : (
                    <AlertTriangle size={10} className="mt-0.5 flex-shrink-0" />
                  )}
                  {result.status === "ok" ? (
                    <div className="flex flex-col gap-0.5">
                      <span>
                        Connected — Gerrit{" "}
                        <strong data-testid="gerrit-wizard-version">
                          {result.version ?? "(unknown version)"}
                        </strong>
                      </span>
                      {result.ssh_host ? (
                        <span className="opacity-80">
                          SSH: <code>{result.ssh_host}:{result.ssh_port}</code>
                        </span>
                      ) : null}
                    </div>
                  ) : (
                    <span>{result.message || "Gerrit SSH probe failed"}</span>
                  )}
                </div>
              )}
            </div>
          </div>

          <div
            data-testid="gerrit-wizard-step-2"
            data-state={step2Badge.toLowerCase()}
            className={`p-3 rounded border space-y-2 ${
              step1Done
                ? "border-[var(--neural-blue)]/40 bg-[var(--background)]"
                : "border-[var(--border)] bg-[var(--background)] opacity-60"
            }`}
          >
            <div className="flex items-start gap-2">
              <span className="flex-shrink-0 w-5 h-5 rounded-full bg-[var(--neural-blue)]/10 text-[var(--neural-blue)] text-[9px] font-mono flex items-center justify-center font-semibold">
                2
              </span>
              <div className="flex-1 min-w-0">
                <div className="font-mono text-[10px] font-semibold text-[var(--foreground)]">
                  Step 2 — SSH key 設定
                </div>
                <div className="font-mono text-[9px] text-[var(--muted-foreground)] leading-relaxed mt-0.5">
                  顯示 OmniSight 的 SSH 公鑰，複製後貼到 Gerrit Settings → SSH Keys。
                </div>
              </div>
              <span
                data-testid="gerrit-wizard-step-2-badge"
                className={`flex-shrink-0 font-mono text-[8px] px-1.5 py-0.5 rounded self-start ${
                  step2Badge === "DONE"
                    ? "bg-[var(--validation-emerald)]/20 text-[var(--validation-emerald)]"
                    : step2Badge === "READY"
                      ? "bg-[var(--neural-blue)]/20 text-[var(--neural-blue)]"
                      : "bg-[var(--secondary)] text-[var(--muted-foreground)]"
                }`}
              >
                {step2Badge}
              </span>
            </div>

            {!step1Done ? (
              <div
                data-testid="gerrit-wizard-step-2-gated"
                className="font-mono text-[9px] text-[var(--muted-foreground)] leading-relaxed pt-1"
              >
                Step 1 通過後解鎖。先完成 Test Connection。
              </div>
            ) : (
              <div className="space-y-1.5 pt-1">
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    data-testid="gerrit-wizard-load-pubkey"
                    onClick={onLoadPubkey}
                    disabled={pubkeyLoading}
                    className="flex items-center gap-1.5 px-2.5 py-1 rounded font-mono text-[10px] bg-[var(--neural-blue)]/10 text-[var(--neural-blue)] hover:bg-[var(--neural-blue)]/20 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                  >
                    {pubkeyLoading ? (
                      <Loader size={10} className="animate-spin" />
                    ) : (
                      <Key size={10} />
                    )}
                    {pubkeyLoading
                      ? "Loading…"
                      : pubkey
                        ? "Reload Public Key"
                        : "Load Public Key"}
                  </button>
                </div>

                {pubkey?.status === "error" && (
                  <div
                    data-testid="gerrit-wizard-pubkey-error"
                    className="font-mono text-[9px] px-2 py-1 rounded flex items-start gap-1.5 bg-[var(--critical-red)]/10 text-[var(--critical-red)]"
                  >
                    <AlertTriangle size={10} className="mt-0.5 flex-shrink-0" />
                    <span>{pubkey.message || "Failed to load SSH public key"}</span>
                  </div>
                )}

                {pubkeyReady && (
                  <>
                    <label
                      htmlFor="gerrit-wizard-pubkey"
                      className="block font-mono text-[9px] text-[var(--muted-foreground)]"
                    >
                      Public key — paste this line into Gerrit Settings → SSH Keys:
                    </label>
                    <textarea
                      id="gerrit-wizard-pubkey"
                      data-testid="gerrit-wizard-pubkey"
                      readOnly
                      value={pubkey?.public_key ?? ""}
                      rows={3}
                      className="w-full font-mono text-[9px] px-2 py-1 rounded bg-[var(--background)] border border-[var(--border)] text-[var(--foreground)] resize-none break-all"
                    />
                    <div className="flex items-center gap-2 flex-wrap">
                      <button
                        type="button"
                        data-testid="gerrit-wizard-copy-pubkey"
                        onClick={onCopyPubkey}
                        className="flex items-center gap-1.5 px-2.5 py-1 rounded font-mono text-[10px] bg-[var(--neural-blue)]/10 text-[var(--neural-blue)] hover:bg-[var(--neural-blue)]/20 transition-colors"
                      >
                        {pubkeyCopied ? (
                          <Check size={10} />
                        ) : (
                          <Copy size={10} />
                        )}
                        {pubkeyCopied ? "Copied" : "Copy"}
                      </button>
                      <button
                        type="button"
                        data-testid="gerrit-wizard-step-2-ack"
                        onClick={() => setStep2Ackd(true)}
                        disabled={step2Ackd}
                        className="flex items-center gap-1.5 px-2.5 py-1 rounded font-mono text-[10px] bg-[var(--validation-emerald)]/10 text-[var(--validation-emerald)] hover:bg-[var(--validation-emerald)]/20 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                      >
                        <Check size={10} />
                        {step2Ackd ? "Added to Gerrit" : "I've added it to Gerrit"}
                      </button>
                    </div>
                    <div
                      data-testid="gerrit-wizard-pubkey-meta"
                      className="font-mono text-[9px] text-[var(--muted-foreground)] leading-relaxed space-y-0.5"
                    >
                      {pubkey?.fingerprint ? (
                        <div>
                          Fingerprint: <code>{pubkey.fingerprint}</code>
                        </div>
                      ) : null}
                      {pubkey?.key_path ? (
                        <div>
                          Source: <code>{pubkey.key_path}</code>
                          {pubkey.key_type ? ` (${pubkey.key_type})` : ""}
                        </div>
                      ) : null}
                      <div>
                        前往 <strong>Gerrit → Settings → SSH Keys → Add Key</strong> 把整行公鑰貼入；
                        貼完按下「I&apos;ve added it to Gerrit」把 Step 2 標記完成。
                      </div>
                    </div>
                  </>
                )}
              </div>
            )}
          </div>

          <div
            data-testid="gerrit-wizard-step-3"
            data-state={step3Badge.toLowerCase()}
            className={`p-3 rounded border space-y-2 ${
              step1Done
                ? "border-[var(--neural-blue)]/40 bg-[var(--background)]"
                : "border-[var(--border)] bg-[var(--background)] opacity-60"
            }`}
          >
            <div className="flex items-start gap-2">
              <span className="flex-shrink-0 w-5 h-5 rounded-full bg-[var(--neural-blue)]/10 text-[var(--neural-blue)] text-[9px] font-mono flex items-center justify-center font-semibold">
                3
              </span>
              <div className="flex-1 min-w-0">
                <div className="font-mono text-[10px] font-semibold text-[var(--foreground)]">
                  Step 3 — merger-agent-bot 帳號
                </div>
                <div className="font-mono text-[9px] text-[var(--muted-foreground)] leading-relaxed mt-0.5">
                  建立 bot 帳號與對應 group（<code>merger-agent-bot</code> + <code>ai-reviewer-bots</code>），
                  構成 O7 雙簽 +2 的 AI 右半邊。Verify 之前需先由 Gerrit admin 執行下方 SSH 指令。
                </div>
              </div>
              <span
                data-testid="gerrit-wizard-step-3-badge"
                className={`flex-shrink-0 font-mono text-[8px] px-1.5 py-0.5 rounded self-start ${
                  step3Badge === "DONE"
                    ? "bg-[var(--validation-emerald)]/20 text-[var(--validation-emerald)]"
                    : step3Badge === "READY"
                      ? "bg-[var(--neural-blue)]/20 text-[var(--neural-blue)]"
                      : "bg-[var(--secondary)] text-[var(--muted-foreground)]"
                }`}
              >
                {step3Badge}
              </span>
            </div>

            {!step1Done ? (
              <div
                data-testid="gerrit-wizard-step-3-gated"
                className="font-mono text-[9px] text-[var(--muted-foreground)] leading-relaxed pt-1"
              >
                Step 1 通過後解鎖。先完成 Test Connection。
              </div>
            ) : (
              <div className="space-y-1.5 pt-1">
                <label
                  htmlFor="gerrit-wizard-bot-commands"
                  className="block font-mono text-[9px] text-[var(--muted-foreground)]"
                >
                  Admin SSH commands — run on any host with admin Gerrit access:
                </label>
                <textarea
                  id="gerrit-wizard-bot-commands"
                  data-testid="gerrit-wizard-bot-commands"
                  readOnly
                  value={botSetupCommands}
                  rows={8}
                  className="w-full font-mono text-[9px] px-2 py-1 rounded bg-[var(--background)] border border-[var(--border)] text-[var(--foreground)] resize-none whitespace-pre"
                />
                <div className="flex items-center gap-2 flex-wrap">
                  <button
                    type="button"
                    data-testid="gerrit-wizard-copy-bot-commands"
                    onClick={onCopyBotCommands}
                    className="flex items-center gap-1.5 px-2.5 py-1 rounded font-mono text-[10px] bg-[var(--neural-blue)]/10 text-[var(--neural-blue)] hover:bg-[var(--neural-blue)]/20 transition-colors"
                  >
                    {botCmdCopied ? <Check size={10} /> : <Copy size={10} />}
                    {botCmdCopied ? "Copied" : "Copy Commands"}
                  </button>
                  <button
                    type="button"
                    data-testid="gerrit-wizard-verify-bot"
                    onClick={onVerifyBot}
                    disabled={botVerifying}
                    className="flex items-center gap-1.5 px-2.5 py-1 rounded font-mono text-[10px] bg-[var(--neural-blue)]/10 text-[var(--neural-blue)] hover:bg-[var(--neural-blue)]/20 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                  >
                    {botVerifying ? (
                      <Loader size={10} className="animate-spin" />
                    ) : (
                      <Users size={10} />
                    )}
                    {botVerifying ? "Verifying…" : "Verify Bot Group"}
                  </button>
                </div>

                {botVerify && (
                  <div
                    data-testid="gerrit-wizard-bot-result"
                    data-status={botVerify.status}
                    className={`font-mono text-[9px] px-2 py-1 rounded flex items-start gap-1.5 ${
                      botGroupReady
                        ? "bg-[var(--validation-emerald)]/10 text-[var(--validation-emerald)]"
                        : "bg-[var(--critical-red)]/10 text-[var(--critical-red)]"
                    }`}
                  >
                    {botGroupReady ? (
                      <Check size={10} className="mt-0.5 flex-shrink-0" />
                    ) : (
                      <AlertTriangle size={10} className="mt-0.5 flex-shrink-0" />
                    )}
                    {botGroupReady ? (
                      <div className="flex flex-col gap-0.5">
                        <span>
                          Group{" "}
                          <strong>{botVerify.group ?? "merger-agent-bot"}</strong>{" "}
                          has{" "}
                          <strong data-testid="gerrit-wizard-bot-member-count">
                            {botVerify.member_count}
                          </strong>{" "}
                          member
                          {(botVerify.member_count ?? 0) === 1 ? "" : "s"}.
                        </span>
                        {botVerify.members && botVerify.members.length > 0 ? (
                          <span
                            data-testid="gerrit-wizard-bot-members"
                            className="opacity-80"
                          >
                            {botVerify.members
                              .map((m) => m.username || m.email || "(unnamed)")
                              .slice(0, 5)
                              .join(", ")}
                            {botVerify.members.length > 5
                              ? ` +${botVerify.members.length - 5} more`
                              : ""}
                          </span>
                        ) : null}
                      </div>
                    ) : (
                      <span>
                        {botVerify.message ||
                          "merger-agent-bot group is not configured"}
                      </span>
                    )}
                  </div>
                )}

                {botGroupReady && (
                  <button
                    type="button"
                    data-testid="gerrit-wizard-step-3-ack"
                    onClick={() => setStep3Ackd(true)}
                    disabled={step3Ackd}
                    className="flex items-center gap-1.5 px-2.5 py-1 rounded font-mono text-[10px] bg-[var(--validation-emerald)]/10 text-[var(--validation-emerald)] hover:bg-[var(--validation-emerald)]/20 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                  >
                    <Check size={10} />
                    {step3Ackd ? "Bot account confirmed" : "I've configured the bot account"}
                  </button>
                )}

                <div className="font-mono text-[9px] text-[var(--muted-foreground)] leading-relaxed">
                  參考 <code>docs/ops/gerrit_dual_two_rule.md §1</code> 了解三個 group 的權責：
                  <code>non-ai-reviewer</code>（人類 hard gate）、<code>ai-reviewer-bots</code>（umbrella）、
                  <code>merger-agent-bot</code>（O6 Merger 專屬）。
                </div>
              </div>
            )}
          </div>

          <div
            data-testid="gerrit-wizard-step-4"
            data-state={step4Badge.toLowerCase()}
            className={`p-3 rounded border space-y-2 ${
              step3Done
                ? "border-[var(--neural-blue)]/40 bg-[var(--background)]"
                : "border-[var(--border)] bg-[var(--background)] opacity-60"
            }`}
          >
            <div className="flex items-start gap-2">
              <span className="flex-shrink-0 w-5 h-5 rounded-full bg-[var(--neural-blue)]/10 text-[var(--neural-blue)] text-[9px] font-mono flex items-center justify-center font-semibold">
                4
              </span>
              <div className="flex-1 min-w-0">
                <div className="font-mono text-[10px] font-semibold text-[var(--foreground)]">
                  Step 4 — submit-rule 驗證
                </div>
                <div className="font-mono text-[9px] text-[var(--muted-foreground)] leading-relaxed mt-0.5">
                  讀取 <code>refs/meta/config:project.config</code> 並核對 O7 雙簽 +2 ACL：
                  <code>ai-reviewer-bots</code> + <code>non-ai-reviewer</code> 可投票，
                  <code>submit</code> 僅開放給 <code>non-ai-reviewer</code>（人類 hard gate）。
                </div>
              </div>
              <span
                data-testid="gerrit-wizard-step-4-badge"
                className={`flex-shrink-0 font-mono text-[8px] px-1.5 py-0.5 rounded self-start ${
                  step4Badge === "DONE"
                    ? "bg-[var(--validation-emerald)]/20 text-[var(--validation-emerald)]"
                    : step4Badge === "READY"
                      ? "bg-[var(--neural-blue)]/20 text-[var(--neural-blue)]"
                      : "bg-[var(--secondary)] text-[var(--muted-foreground)]"
                }`}
              >
                {step4Badge}
              </span>
            </div>

            {!step3Done ? (
              <div
                data-testid="gerrit-wizard-step-4-gated"
                className="font-mono text-[9px] text-[var(--muted-foreground)] leading-relaxed pt-1"
              >
                Step 3 通過後解鎖。先確認 <code>merger-agent-bot</code> group 已就緒。
              </div>
            ) : (
              <div className="space-y-1.5 pt-1">
                <div className="flex items-center gap-2">
                  <label
                    htmlFor="gerrit-wizard-submit-rule-project"
                    className="font-mono text-[9px] text-[var(--muted-foreground)] w-20 shrink-0"
                  >
                    Project
                  </label>
                  <input
                    id="gerrit-wizard-submit-rule-project"
                    data-testid="gerrit-wizard-submit-rule-project"
                    type="text"
                    autoComplete="off"
                    spellCheck={false}
                    value={submitRuleProject}
                    onChange={(e) => {
                      setSubmitRuleProject(e.target.value)
                      setSubmitRuleVerify(null)
                    }}
                    placeholder="omnisight-productizer"
                    className="flex-1 font-mono text-[10px] px-2 py-1 rounded bg-[var(--background)] border border-[var(--border)] text-[var(--foreground)] focus:outline-none focus:border-[var(--neural-blue)]"
                  />
                </div>
                {submitRuleProject.trim().length > 0 && !submitRuleProjectValid && (
                  <div
                    data-testid="gerrit-wizard-submit-rule-project-invalid"
                    className="font-mono text-[9px] text-[var(--critical-red)]"
                  >
                    Project must be letters/digits/_/-/./ and start with a word character.
                  </div>
                )}
                <div className="flex items-center gap-2 pt-1">
                  <button
                    type="button"
                    data-testid="gerrit-wizard-verify-submit-rule"
                    onClick={onVerifySubmitRule}
                    disabled={!canVerifySubmitRule}
                    className="flex items-center gap-1.5 px-2.5 py-1 rounded font-mono text-[10px] bg-[var(--neural-blue)]/10 text-[var(--neural-blue)] hover:bg-[var(--neural-blue)]/20 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                  >
                    {submitRuleVerifying ? (
                      <Loader size={10} className="animate-spin" />
                    ) : (
                      <ShieldCheck size={10} />
                    )}
                    {submitRuleVerifying ? "Verifying…" : "Verify Submit-Rule"}
                  </button>
                </div>

                {submitRuleVerify && (
                  <div
                    data-testid="gerrit-wizard-submit-rule-result"
                    data-status={submitRuleVerify.status}
                    className={`font-mono text-[9px] px-2 py-1 rounded flex items-start gap-1.5 ${
                      submitRuleReady
                        ? "bg-[var(--validation-emerald)]/10 text-[var(--validation-emerald)]"
                        : "bg-[var(--critical-red)]/10 text-[var(--critical-red)]"
                    }`}
                  >
                    {submitRuleReady ? (
                      <Check size={10} className="mt-0.5 flex-shrink-0" />
                    ) : (
                      <AlertTriangle size={10} className="mt-0.5 flex-shrink-0" />
                    )}
                    {submitRuleReady ? (
                      <span>
                        Project{" "}
                        <strong>
                          {submitRuleVerify.project ?? submitRuleProject.trim()}
                        </strong>{" "}
                        carries the dual-+2 rule. 三項 ACL 全部符合。
                      </span>
                    ) : (
                      <span>
                        {submitRuleVerify.message ||
                          "Gerrit submit-rule verification failed"}
                      </span>
                    )}
                  </div>
                )}

                {submitRuleChecks.length > 0 && (
                  <ul
                    data-testid="gerrit-wizard-submit-rule-checks"
                    className="space-y-0.5 list-none p-0 m-0"
                  >
                    {submitRuleChecks.map((c) => (
                      <li
                        key={c.id}
                        data-testid={`gerrit-wizard-submit-rule-check-${c.id}`}
                        data-ok={c.ok ? "true" : "false"}
                        className={`font-mono text-[9px] leading-relaxed flex items-start gap-1.5 ${
                          c.ok
                            ? "text-[var(--validation-emerald)]"
                            : "text-[var(--critical-red)]"
                        }`}
                      >
                        {c.ok ? (
                          <Check size={10} className="mt-0.5 flex-shrink-0" />
                        ) : (
                          <AlertTriangle size={10} className="mt-0.5 flex-shrink-0" />
                        )}
                        <span>
                          <strong>{c.id}</strong>
                          {c.ok ? " — ok" : `: ${c.detail || "missing"}`}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}

                {submitRuleReady && (
                  <button
                    type="button"
                    data-testid="gerrit-wizard-step-4-ack"
                    onClick={() => setStep4Ackd(true)}
                    disabled={step4Ackd}
                    className="flex items-center gap-1.5 px-2.5 py-1 rounded font-mono text-[10px] bg-[var(--validation-emerald)]/10 text-[var(--validation-emerald)] hover:bg-[var(--validation-emerald)]/20 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                  >
                    <Check size={10} />
                    {step4Ackd ? "Submit-rule confirmed" : "Submit-rule 已套用"}
                  </button>
                )}

                <div className="font-mono text-[9px] text-[var(--muted-foreground)] leading-relaxed">
                  未通過時，請參考 <code>.gerrit/project.config.example</code> 與{" "}
                  <code>docs/ops/gerrit_dual_two_rule.md §2</code> 在{" "}
                  <code>refs/meta/config</code> 上推送對應的規則。
                </div>
              </div>
            )}
          </div>

          <div
            data-testid="gerrit-wizard-step-5"
            data-state={step5Badge.toLowerCase()}
            className={`p-3 rounded border space-y-2 ${
              step4Done
                ? "border-[var(--neural-blue)]/40 bg-[var(--background)]"
                : "border-[var(--border)] bg-[var(--background)] opacity-60"
            }`}
          >
            <div className="flex items-start gap-2">
              <span className="flex-shrink-0 w-5 h-5 rounded-full bg-[var(--neural-blue)]/10 text-[var(--neural-blue)] text-[9px] font-mono flex items-center justify-center font-semibold">
                5
              </span>
              <div className="flex-1 min-w-0">
                <div className="font-mono text-[10px] font-semibold text-[var(--foreground)]">
                  Step 5 — Webhook 設定
                </div>
                <div className="font-mono text-[9px] text-[var(--muted-foreground)] leading-relaxed mt-0.5">
                  顯示 inbound webhook URL + HMAC-SHA256 secret，貼到 Gerrit{" "}
                  <code>refs/meta/config:webhooks.config</code> 的{" "}
                  <code>[remote ...]</code> 區塊以接收 patchset / comment / merged 事件。
                </div>
              </div>
              <span
                data-testid="gerrit-wizard-step-5-badge"
                className={`flex-shrink-0 font-mono text-[8px] px-1.5 py-0.5 rounded self-start ${
                  step5Badge === "DONE"
                    ? "bg-[var(--validation-emerald)]/20 text-[var(--validation-emerald)]"
                    : step5Badge === "READY"
                      ? "bg-[var(--neural-blue)]/20 text-[var(--neural-blue)]"
                      : "bg-[var(--secondary)] text-[var(--muted-foreground)]"
                }`}
              >
                {step5Badge}
              </span>
            </div>

            {!step4Done ? (
              <div
                data-testid="gerrit-wizard-step-5-gated"
                className="font-mono text-[9px] text-[var(--muted-foreground)] leading-relaxed pt-1"
              >
                Step 4 通過後解鎖。先確認 <code>refs/meta/config:project.config</code>{" "}
                帶有 dual-+2 ACL，否則 webhook 啟用後會收到 Gerrit 拒簽的事件。
              </div>
            ) : (
              <div className="space-y-1.5 pt-1">
                {!webhookInfo && (
                  <button
                    type="button"
                    data-testid="gerrit-wizard-load-webhook-info"
                    onClick={onLoadWebhookInfo}
                    disabled={webhookInfoLoading}
                    className="flex items-center gap-1.5 px-2.5 py-1 rounded font-mono text-[10px] bg-[var(--neural-blue)]/10 text-[var(--neural-blue)] hover:bg-[var(--neural-blue)]/20 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                  >
                    {webhookInfoLoading ? (
                      <Loader size={10} className="animate-spin" />
                    ) : (
                      <Webhook size={10} />
                    )}
                    {webhookInfoLoading ? "Loading…" : "Load webhook info"}
                  </button>
                )}

                {webhookInfo && webhookInfo.status === "error" && (
                  <div
                    data-testid="gerrit-wizard-webhook-info-error"
                    className="font-mono text-[9px] px-2 py-1 rounded flex items-start gap-1.5 bg-[var(--critical-red)]/10 text-[var(--critical-red)]"
                  >
                    <AlertTriangle size={10} className="mt-0.5 flex-shrink-0" />
                    <span>{webhookInfo.message || "Failed to load webhook info"}</span>
                  </div>
                )}

                {webhookInfo && webhookInfo.status === "ok" && webhookInfo.webhook_url && (
                  <>
                    <div className="space-y-0.5">
                      <div className="font-mono text-[9px] text-[var(--muted-foreground)]">
                        Webhook URL（貼到 <code>url = ...</code>）
                      </div>
                      <div className="flex items-center gap-2">
                        <code
                          data-testid="gerrit-wizard-webhook-url"
                          className="flex-1 font-mono text-[10px] px-2 py-1 rounded bg-[var(--secondary)] text-[var(--foreground)] break-all"
                        >
                          {webhookInfo.webhook_url}
                        </code>
                        <button
                          type="button"
                          data-testid="gerrit-wizard-copy-webhook-url"
                          onClick={onCopyWebhookUrl}
                          className="flex items-center gap-1 px-2 py-1 rounded font-mono text-[9px] bg-[var(--neural-blue)]/10 text-[var(--neural-blue)] hover:bg-[var(--neural-blue)]/20 transition-colors"
                        >
                          <Copy size={10} />
                          {webhookUrlCopied ? "Copied" : "Copy"}
                        </button>
                      </div>
                    </div>

                    <div className="space-y-0.5 pt-1">
                      <div className="font-mono text-[9px] text-[var(--muted-foreground)]">
                        HMAC secret（貼到 <code>secret = ...</code>）
                      </div>
                      {webhookInfo.secret_configured ? (
                        <div className="flex items-center gap-2">
                          <code
                            data-testid="gerrit-wizard-webhook-secret-masked"
                            className="flex-1 font-mono text-[10px] px-2 py-1 rounded bg-[var(--secondary)] text-[var(--foreground)]"
                          >
                            {webhookInfo.secret_masked || "configured"}
                          </code>
                          <span
                            data-testid="gerrit-wizard-webhook-secret-status"
                            className="flex-shrink-0 font-mono text-[9px] text-[var(--validation-emerald)]"
                          >
                            configured
                          </span>
                        </div>
                      ) : (
                        <div
                          data-testid="gerrit-wizard-webhook-secret-empty"
                          className="font-mono text-[9px] text-[var(--hardware-orange)]"
                        >
                          目前尚未設定 secret — 點下方按鈕產生一組新的。
                        </div>
                      )}
                    </div>

                    {webhookSecretPlain && (
                      <div className="space-y-0.5 pt-1 p-2 rounded border border-[var(--validation-emerald)]/40 bg-[var(--validation-emerald)]/5">
                        <div className="font-mono text-[9px] text-[var(--validation-emerald)] font-semibold">
                          NEW SECRET — 僅顯示一次，請立即複製
                        </div>
                        <div className="flex items-center gap-2">
                          <code
                            data-testid="gerrit-wizard-webhook-secret-plain"
                            className="flex-1 font-mono text-[10px] px-2 py-1 rounded bg-[var(--background)] text-[var(--foreground)] break-all"
                          >
                            {webhookSecretPlain}
                          </code>
                          <button
                            type="button"
                            data-testid="gerrit-wizard-copy-webhook-secret"
                            onClick={onCopyWebhookSecret}
                            className="flex items-center gap-1 px-2 py-1 rounded font-mono text-[9px] bg-[var(--validation-emerald)]/10 text-[var(--validation-emerald)] hover:bg-[var(--validation-emerald)]/20 transition-colors"
                          >
                            <Copy size={10} />
                            {webhookSecretCopied ? "Copied" : "Copy"}
                          </button>
                        </div>
                      </div>
                    )}

                    {webhookGenError && (
                      <div
                        data-testid="gerrit-wizard-webhook-gen-error"
                        className="font-mono text-[9px] px-2 py-1 rounded flex items-start gap-1.5 bg-[var(--critical-red)]/10 text-[var(--critical-red)]"
                      >
                        <AlertTriangle size={10} className="mt-0.5 flex-shrink-0" />
                        <span>{webhookGenError}</span>
                      </div>
                    )}

                    <div className="flex items-center gap-2 pt-1">
                      <button
                        type="button"
                        data-testid="gerrit-wizard-generate-webhook-secret"
                        onClick={onGenerateWebhookSecret}
                        disabled={webhookGenerating}
                        className="flex items-center gap-1.5 px-2.5 py-1 rounded font-mono text-[10px] bg-[var(--neural-blue)]/10 text-[var(--neural-blue)] hover:bg-[var(--neural-blue)]/20 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                      >
                        {webhookGenerating ? (
                          <Loader size={10} className="animate-spin" />
                        ) : (
                          <Key size={10} />
                        )}
                        {webhookGenerating
                          ? "Generating…"
                          : webhookInfo.secret_configured
                            ? "Rotate secret"
                            : "Generate secret"}
                      </button>
                      {webhookSecretReady && (
                        <button
                          type="button"
                          data-testid="gerrit-wizard-step-5-ack"
                          onClick={() => setStep5Ackd(true)}
                          disabled={step5Ackd}
                          className="flex items-center gap-1.5 px-2.5 py-1 rounded font-mono text-[10px] bg-[var(--validation-emerald)]/10 text-[var(--validation-emerald)] hover:bg-[var(--validation-emerald)]/20 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                        >
                          <Check size={10} />
                          {step5Ackd ? "Webhook confirmed" : "Webhook 已套用"}
                        </button>
                      )}
                    </div>

                    <div className="font-mono text-[9px] text-[var(--muted-foreground)] leading-relaxed pt-1">
                      Gerrit 端設定範本（push 到 <code>refs/meta/config:webhooks.config</code>）：
                    </div>
                    <pre
                      data-testid="gerrit-wizard-webhook-config-snippet"
                      className="font-mono text-[9px] px-2 py-1 rounded bg-[var(--secondary)] text-[var(--foreground)] overflow-x-auto whitespace-pre"
                    >{`[remote "omnisight"]\n  url = ${webhookInfo.webhook_url}\n  secret = <paste-the-secret-here>\n  event = patchset-created\n  event = comment-added\n  event = change-merged\n  sslVerify = true`}</pre>
                    <div className="font-mono text-[9px] text-[var(--muted-foreground)] leading-relaxed">
                      Signature header：<code>{webhookInfo.signature_header || "X-Gerrit-Signature"}</code>{" "}
                      / algo：<code>{webhookInfo.signature_algorithm || "hmac-sha256"}</code>。Rotate
                      會立即作廢舊 secret，記得同步更新 Gerrit 端。
                    </div>
                  </>
                )}
              </div>
            )}
          </div>

          <div
            data-testid="gerrit-wizard-finalize"
            data-state={
              finalizeResult?.enabled ? "done" : step5Ackd ? "ready" : "pending"
            }
            className={`p-3 rounded border space-y-2 ${
              step5Ackd
                ? "border-[var(--validation-emerald)]/50 bg-[var(--background)]"
                : "border-[var(--border)] bg-[var(--background)] opacity-60"
            }`}
          >
            <div className="flex items-start gap-2">
              <span className="flex-shrink-0 w-5 h-5 rounded-full bg-[var(--validation-emerald)]/10 text-[var(--validation-emerald)] text-[9px] font-mono flex items-center justify-center font-semibold">
                ✓
              </span>
              <div className="flex-1 min-w-0">
                <div className="font-mono text-[10px] font-semibold text-[var(--foreground)]">
                  Finalize — 寫入 config 並啟用 Gerrit
                </div>
                <div className="font-mono text-[9px] text-[var(--muted-foreground)] leading-relaxed mt-0.5">
                  把 Steps 1–5 收集到的 SSH endpoint / REST URL / project 寫入{" "}
                  <code>settings.gerrit_*</code>，並把 <code>gerrit_enabled</code> 翻成 true。
                </div>
              </div>
              <span
                data-testid="gerrit-wizard-finalize-badge"
                className={`flex-shrink-0 font-mono text-[8px] px-1.5 py-0.5 rounded self-start ${
                  finalizeResult?.enabled
                    ? "bg-[var(--validation-emerald)]/20 text-[var(--validation-emerald)]"
                    : step5Ackd
                      ? "bg-[var(--neural-blue)]/20 text-[var(--neural-blue)]"
                      : "bg-[var(--secondary)] text-[var(--muted-foreground)]"
                }`}
              >
                {finalizeResult?.enabled
                  ? "ENABLED"
                  : step5Ackd
                    ? "READY"
                    : "PENDING"}
              </span>
            </div>

            {!step5Ackd ? (
              <div
                data-testid="gerrit-wizard-finalize-gated"
                className="font-mono text-[9px] text-[var(--muted-foreground)] leading-relaxed pt-1"
              >
                完成 Step 5 (Webhook 已套用) 後解鎖。
              </div>
            ) : (
              <div className="space-y-1.5 pt-1">
                <div
                  data-testid="gerrit-wizard-finalize-summary"
                  className="font-mono text-[9px] text-[var(--muted-foreground)] leading-relaxed space-y-0.5 p-2 rounded bg-[var(--secondary)]"
                >
                  <div>
                    SSH:{" "}
                    <code className="text-[var(--foreground)]">
                      {sshHost.trim() || "(not set)"}:{parsedPort}
                    </code>
                  </div>
                  {url.trim() && (
                    <div>
                      REST URL:{" "}
                      <code className="text-[var(--foreground)]">{url.trim()}</code>
                    </div>
                  )}
                  {submitRuleProject.trim() && (
                    <div>
                      Project:{" "}
                      <code className="text-[var(--foreground)]">
                        {submitRuleProject.trim()}
                      </code>
                    </div>
                  )}
                </div>

                {!finalizeResult?.enabled && (
                  <div className="flex items-center gap-2 pt-1">
                    <button
                      type="button"
                      data-testid="gerrit-wizard-finalize-button"
                      onClick={onFinalize}
                      disabled={finalizing || !sshHost.trim() || !portValid}
                      className="flex items-center gap-1.5 px-2.5 py-1 rounded font-mono text-[10px] bg-[var(--validation-emerald)]/10 text-[var(--validation-emerald)] hover:bg-[var(--validation-emerald)]/20 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                    >
                      {finalizing ? (
                        <Loader size={10} className="animate-spin" />
                      ) : (
                        <Check size={10} />
                      )}
                      {finalizing ? "Enabling…" : "啟用 Gerrit 整合"}
                    </button>
                  </div>
                )}

                {finalizeError && (
                  <div
                    data-testid="gerrit-wizard-finalize-error"
                    className="font-mono text-[9px] px-2 py-1 rounded flex items-start gap-1.5 bg-[var(--critical-red)]/10 text-[var(--critical-red)]"
                  >
                    <AlertTriangle size={10} className="mt-0.5 flex-shrink-0" />
                    <span>{finalizeError}</span>
                  </div>
                )}

                {finalizeResult?.enabled && (
                  <div
                    data-testid="gerrit-wizard-finalize-success"
                    className="font-mono text-[10px] px-2 py-1.5 rounded flex items-start gap-1.5 bg-[var(--validation-emerald)]/15 text-[var(--validation-emerald)] border border-[var(--validation-emerald)]/40"
                  >
                    <Check size={12} className="mt-0.5 flex-shrink-0" />
                    <div className="flex flex-col gap-0.5">
                      <span className="font-semibold">
                        {finalizeResult.message || "Gerrit 整合已啟用"}
                      </span>
                      {finalizeResult.note && (
                        <span className="opacity-80 text-[9px]">
                          {finalizeResult.note}
                        </span>
                      )}
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        </div>

        <div className="flex justify-end px-4 py-2 border-t border-[var(--border)] bg-[var(--secondary)]">
          <button
            onClick={onClose}
            className="px-3 py-1 rounded font-mono text-[10px] bg-[var(--neural-blue)] text-black font-semibold hover:opacity-90 transition-colors"
          >
            CLOSE
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}

export function IntegrationSettings({ open, onClose }: IntegrationSettingsProps) {
  const [settingsData, setSettingsData] = useState<Record<string, Record<string, unknown>>>({})
  const [dirty, setDirty] = useState<Record<string, string | number | boolean>>({})
  const [saving, setSaving] = useState(false)
  const [providers, setProviders] = useState<api.ProviderConfig[]>([])
  const [gerritWizardOpen, setGerritWizardOpen] = useState(false)

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
      category === "webhooks" ? "github_webhook" :
      category === "ci" ? "ci" : ""
    }_${field}`
    if (key in dirty) return String(dirty[key])
    return String(settingsData[category]?.[field] ?? "")
  }

  // B14 Part D — per-tab connection-status badge derivation. "configured" is
  // a passive signal (fields are populated OR the user has staged an edit);
  // we intentionally avoid proactive probes here since each SettingsSection
  // already has a TEST button for active validation. The badge exists to
  // give at-a-glance "is there anything set" feedback when the user opens
  // the modal, so three-state (ok / warn / error) collapses to two states
  // (configured / not-configured) until a probe explicitly fails.
  const hasValue = (category: string, field: string, dirtyKey?: string): boolean => {
    const v = getVal(category, field, dirtyKey).trim()
    // Masked backend responses render as "***" — treat as configured.
    return v.length > 0 && v !== "false" && v !== "0"
  }
  const tabStatus = {
    git: (
      ((settingsData["git"]?.["credentials"] as unknown[] | undefined)?.length ?? 0) > 0 ||
      hasValue("git", "ssh_key_path") ||
      hasValue("git", "github_token", "github_token") ||
      hasValue("git", "gitlab_token", "gitlab_token")
    ),
    gerrit: getVal("gerrit", "enabled") === "true" && hasValue("gerrit", "url"),
    webhooks: (
      hasValue("webhooks", "github_secret", "github_webhook_secret") ||
      hasValue("webhooks", "gitlab_secret", "gitlab_webhook_secret") ||
      hasValue("webhooks", "gerrit_secret") ||
      hasValue("webhooks", "jira_secret", "jira_webhook_secret") ||
      hasValue("jira", "url") ||
      hasValue("slack", "webhook")
    ),
    cicd: (
      getVal("ci", "github_actions_enabled") === "true" ||
      getVal("ci", "jenkins_enabled") === "true" ||
      getVal("ci", "gitlab_ci_enabled", "ci_gitlab_enabled") === "true"
    ),
  }
  const badgeClass = (ok: boolean) =>
    ok
      ? "bg-[var(--validation-emerald)]"
      : "bg-[var(--hardware-orange)]/60"
  const badgeTitle = (ok: boolean) =>
    ok ? "connected / configured" : "not configured"

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

          {/* B14 Part D row 228 — split the former single-page Integration
              form into four focused tabs. Git-forge credentials, Gerrit
              wizard/settings, inbound webhook secrets, and outbound CI
              triggers each have their own tab so first-time users don't
              have to scroll past dozens of unrelated fields. Each
              TabsTrigger carries a passive "configured / not-configured"
              badge derived from `tabStatus` above so the user can see at a
              glance which tabs still need attention. Active probe results
              live inside the per-section TEST button. */}
          <Tabs defaultValue="git" className="w-full gap-2">
            <TabsList className="h-auto w-full grid grid-cols-4 bg-[var(--background)] border border-[var(--border)] p-0.5">
              <TabsTrigger
                value="git"
                className="font-mono text-[10px] tracking-fui py-1 data-[state=active]:bg-[var(--neural-blue)]/10 data-[state=active]:text-[var(--neural-blue)]"
              >
                <span
                  className={`inline-block w-1.5 h-1.5 rounded-full ${badgeClass(tabStatus.git)}`}
                  title={badgeTitle(tabStatus.git)}
                />
                GIT
              </TabsTrigger>
              <TabsTrigger
                value="gerrit"
                className="font-mono text-[10px] tracking-fui py-1 data-[state=active]:bg-[var(--neural-blue)]/10 data-[state=active]:text-[var(--neural-blue)]"
              >
                <span
                  className={`inline-block w-1.5 h-1.5 rounded-full ${badgeClass(tabStatus.gerrit)}`}
                  title={badgeTitle(tabStatus.gerrit)}
                />
                GERRIT
              </TabsTrigger>
              <TabsTrigger
                value="webhooks"
                className="font-mono text-[10px] tracking-fui py-1 data-[state=active]:bg-[var(--neural-blue)]/10 data-[state=active]:text-[var(--neural-blue)]"
              >
                <span
                  className={`inline-block w-1.5 h-1.5 rounded-full ${badgeClass(tabStatus.webhooks)}`}
                  title={badgeTitle(tabStatus.webhooks)}
                />
                WEBHOOKS
              </TabsTrigger>
              <TabsTrigger
                value="cicd"
                className="font-mono text-[10px] tracking-fui py-1 data-[state=active]:bg-[var(--neural-blue)]/10 data-[state=active]:text-[var(--neural-blue)]"
              >
                <span
                  className={`inline-block w-1.5 h-1.5 rounded-full ${badgeClass(tabStatus.cicd)}`}
                  title={badgeTitle(tabStatus.cicd)}
                />
                CI/CD
              </TabsTrigger>
            </TabsList>

            {/* Tab 1 — Git forges (GitHub / GitLab / SSH) + multi-instance map */}
            <TabsContent value="git" className="space-y-2 mt-0">
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
            </TabsContent>

            {/* Tab 2 — Gerrit Code Review (settings + wizard entry).
                `forceMount` + `data-[state=inactive]:hidden` keeps the Setup
                Wizard button in the DOM even when the Gerrit tab is not
                active so the GerritSetupWizardDialog can still be opened by
                code paths (e.g. deep-links, test harnesses) that assume a
                flat layout. Radix flips the `hidden` attribute on inactive
                panels so assistive tech still sees only the active tab. */}
            <TabsContent value="gerrit" forceMount className="space-y-2 mt-0 data-[state=inactive]:hidden">
              <SettingsSection title="GERRIT CODE REVIEW" integration="gerrit">
                {/* B14 Part C row 221: entry-point button that opens the Gerrit
                    Setup Wizard modal. The 5-step interactive content (URL+SSH
                    test / SSH-key hint / merger-agent-bot / submit-rule probe /
                    webhook wiring) is scaffolded inside the modal and fleshed
                    out in subsequent Part C rows. */}
                <div className="flex items-center justify-between gap-2">
                  <span className="font-mono text-[9px] text-[var(--muted-foreground)] leading-tight">
                    New to Gerrit? Open the guided walkthrough.
                  </span>
                  <button
                    onClick={() => setGerritWizardOpen(true)}
                    className="flex items-center gap-1 px-2 py-1 rounded font-mono text-[9px] bg-[var(--neural-blue)]/10 text-[var(--neural-blue)] hover:bg-[var(--neural-blue)]/20 transition-colors"
                    title="Open a 5-step guided walkthrough for Gerrit Code Review integration"
                  >
                    <Settings size={10} /> SETUP WIZARD
                  </button>
                </div>
                <ToggleField label="Enabled" value={getVal("gerrit", "enabled") === "true"} onChange={v => setVal("gerrit_enabled", v)} />
                <SettingField label="URL" value={getVal("gerrit", "url")} onChange={v => setVal("gerrit_url", v)} />
                <SettingField label="SSH Host" value={getVal("gerrit", "ssh_host")} onChange={v => setVal("gerrit_ssh_host", v)} />
                <SettingField label="SSH Port" value={getVal("gerrit", "ssh_port")} onChange={v => setVal("gerrit_ssh_port", v)} />
                <SettingField label="Project" value={getVal("gerrit", "project")} onChange={v => setVal("gerrit_project", v)} />
                {/* B14 Part D row 233 — surface the last Gerrit Code Review
                    scalar that wasn't previously editable in the UI. CSV of
                    git-remote names post-merge push fan-out targets; read +
                    written by backend/routers/webhooks.py:295 and whitelisted
                    for PUT in backend/routers/integration.py _UPDATABLE_FIELDS.
                    `gerrit_webhook_secret` is NOT rendered here by design —
                    it's rotate-only via the Setup Wizard Step 5, so exposing
                    a plaintext input would let users overwrite a rotated
                    secret and silently break event signature verification. */}
                <SettingField label="Replication Targets" value={getVal("gerrit", "replication_targets")} onChange={v => setVal("gerrit_replication_targets", v)} />
              </SettingsSection>
            </TabsContent>

            {/* Tab 3 — Webhook secrets + issue/notification integrations. Jira
                and Slack land here because they are webhook-driven outbound
                channels — keeping them with the inbound secrets avoids
                scattering "webhook" URLs across multiple tabs.

                B14 Part D row 234: every inbound webhook secret gets a
                per-field status dot (green = configured / grey = empty) so an
                operator can tell at a glance which forges are wired up
                without squinting at the masked password field. Gerrit is
                rendered as a READ-ONLY row with a "ROTATE IN WIZARD" action
                — exposing a plaintext input would let users silently
                overwrite a rotated secret and break Gerrit event signature
                verification. */}
            <TabsContent value="webhooks" className="space-y-2 mt-0">
              <SettingsSection title="INBOUND WEBHOOK SECRETS">
                {([
                  { label: "GitHub Secret", category: "webhooks", field: "github_secret", dirtyKey: "github_webhook_secret" },
                  { label: "GitLab Secret", category: "webhooks", field: "gitlab_secret", dirtyKey: "gitlab_webhook_secret" },
                  { label: "Jira Secret", category: "webhooks", field: "jira_secret", dirtyKey: "jira_webhook_secret" },
                ] as const).map(({ label, category, field, dirtyKey }) => {
                  const configured = hasValue(category, field, dirtyKey)
                  return (
                    <div key={dirtyKey} className="flex items-center gap-2">
                      <label
                        className="font-mono text-[9px] text-[var(--muted-foreground)] w-20 shrink-0 flex items-center gap-1"
                        data-testid={`webhook-secret-label-${dirtyKey}`}
                      >
                        <span
                          className={`w-1.5 h-1.5 rounded-full ${configured ? "bg-[var(--validation-emerald)]" : "bg-[var(--muted-foreground)]/30"}`}
                          title={badgeTitle(configured)}
                          data-testid={`webhook-secret-dot-${dirtyKey}`}
                        />
                        {label}
                      </label>
                      <input
                        type="password"
                        value={getVal(category, field, dirtyKey)}
                        onChange={e => setVal(dirtyKey, e.target.value)}
                        placeholder={configured ? "••• configured •••" : "paste secret here"}
                        data-testid={`webhook-secret-input-${dirtyKey}`}
                        className="flex-1 font-mono text-[10px] px-2 py-1 rounded bg-[var(--background)] border border-[var(--border)] text-[var(--foreground)] placeholder:text-[var(--muted-foreground)]/50 focus:outline-none focus:border-[var(--neural-blue)]"
                      />
                    </div>
                  )
                })}
                {(() => {
                  const gerritConfigured = hasValue("webhooks", "gerrit_secret")
                  return (
                    <div className="flex items-center gap-2" data-testid="webhook-secret-row-gerrit">
                      <label className="font-mono text-[9px] text-[var(--muted-foreground)] w-20 shrink-0 flex items-center gap-1">
                        <span
                          className={`w-1.5 h-1.5 rounded-full ${gerritConfigured ? "bg-[var(--validation-emerald)]" : "bg-[var(--muted-foreground)]/30"}`}
                          title={badgeTitle(gerritConfigured)}
                          data-testid="webhook-secret-dot-gerrit"
                        />
                        Gerrit Secret
                      </label>
                      <div className="flex-1 flex items-center gap-2">
                        <span
                          className="flex-1 font-mono text-[10px] px-2 py-1 rounded bg-[var(--background)]/50 border border-dashed border-[var(--border)] text-[var(--muted-foreground)] select-none"
                          data-testid="webhook-secret-status-gerrit"
                        >
                          {gerritConfigured ? "••• configured (rotate-only) •••" : "not configured"}
                        </span>
                        <button
                          onClick={() => setGerritWizardOpen(true)}
                          className="px-2 py-1 rounded font-mono text-[9px] bg-[var(--neural-blue)]/10 text-[var(--neural-blue)] hover:bg-[var(--neural-blue)]/20 transition-colors shrink-0"
                          title="Gerrit webhook secret is rotate-only — open Setup Wizard Step 5"
                          data-testid="webhook-secret-rotate-gerrit"
                        >
                          ROTATE IN WIZARD
                        </button>
                      </div>
                    </div>
                  )
                })()}
                <div className="font-mono text-[8px] text-[var(--muted-foreground)]/70 pt-1 leading-tight">
                  Gerrit secret is never rendered in plaintext — rotating is one-way via the Setup Wizard (Step 5).
                </div>
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
            </TabsContent>

            {/* Tab 4 — Outbound CI/CD triggers. Config fields already exist in
                backend/config.py (ci_*) and are whitelisted in
                backend/routers/integration.py `_UPDATABLE_FIELDS`; this is the
                first time they are surfaced in the UI. */}
            <TabsContent value="cicd" className="space-y-2 mt-0">
              <SettingsSection title="GITHUB ACTIONS">
                <ToggleField
                  label="Enabled"
                  value={getVal("ci", "github_actions_enabled") === "true"}
                  onChange={v => setVal("ci_github_actions_enabled", v)}
                />
                <div className="font-mono text-[8px] text-[var(--muted-foreground)]/70 pt-1 leading-tight">
                  Uses the GitHub Token from the Git tab.
                </div>
              </SettingsSection>

              <SettingsSection title="JENKINS">
                <ToggleField
                  label="Enabled"
                  value={getVal("ci", "jenkins_enabled") === "true"}
                  onChange={v => setVal("ci_jenkins_enabled", v)}
                />
                <SettingField
                  label="URL"
                  value={getVal("ci", "jenkins_url")}
                  onChange={v => setVal("ci_jenkins_url", v)}
                />
                <SettingField
                  label="User"
                  value={getVal("ci", "jenkins_user", "ci_jenkins_user")}
                  onChange={v => setVal("ci_jenkins_user", v)}
                />
                <SettingField
                  label="API Token"
                  type="password"
                  value={getVal("ci", "jenkins_api_token", "ci_jenkins_api_token")}
                  onChange={v => setVal("ci_jenkins_api_token", v)}
                />
              </SettingsSection>

              <SettingsSection title="GITLAB CI">
                <ToggleField
                  label="Enabled"
                  value={getVal("ci", "gitlab_ci_enabled", "ci_gitlab_enabled") === "true"}
                  onChange={v => setVal("ci_gitlab_enabled", v)}
                />
                <div className="font-mono text-[8px] text-[var(--muted-foreground)]/70 pt-1 leading-tight">
                  Uses the GitLab URL + Token from the Git tab.
                </div>
              </SettingsSection>
            </TabsContent>
          </Tabs>

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
      <GerritSetupWizardDialog open={gerritWizardOpen} onClose={() => setGerritWizardOpen(false)} />
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
