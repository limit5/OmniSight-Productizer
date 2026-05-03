"use client"

import { useState, useEffect, useCallback, useRef } from "react"
import { createPortal } from "react-dom"
import { Settings, X, Check, AlertTriangle, Loader, ChevronDown, ChevronUp, WifiOff, Key, Plus, Trash2, HardDrive, RefreshCw, Copy, Users, ShieldCheck, Webhook } from "lucide-react"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import * as api from "@/lib/api"
import { useTenantOptional } from "@/lib/tenant-context"
import { useProjectOptional } from "@/lib/project-context"
import { OllamaToolCallingBadge } from "@/components/omnisight/ollama-tool-calling-badge"
import { OllamaToolFailureAlert } from "@/components/omnisight/ollama-tool-failure-alert"

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

function SettingsSection({ title, integration, status, statusTestId, onTestResult, onTest, children }: {
  title: string
  integration?: string
  // B14 Part D row 235 — optional per-section status dot. Pass `true` to
  // render a green dot (configured), `false` to render a grey dot (not
  // configured), or omit entirely (no dot). Today only the CI/CD tab's
  // three sections use this — other sections stay dotless to avoid visual
  // noise. `statusTestId` provides a stable `data-testid` for vitest.
  status?: boolean
  statusTestId?: string
  // B14 Part D row 236 — lift test outcomes up to the parent so the
  // top-of-tab connection badge can flip from ⚠️ "not configured" /
  // ✅ "connected" into ❌ "error" when an active probe (the TEST button)
  // fails. The callback fires on every probe resolution — success and
  // failure alike — so the parent maintains a persistent view of the
  // last known state per integration even while the section is collapsed.
  onTestResult?: (integration: string, result: TestResult) => void
  // 2026-04-22: optional probe override. Default TEST behaviour calls
  // ``api.testIntegration(integration)`` which reads from
  // ``settings.*`` on the backend — meaning a token typed into the
  // form but NOT YET SAVED fails with "<provider> token not set"
  // because the backend doesn't know about your unsaved dirty value.
  // Call sites that want TEST to probe the currently-typed candidate
  // (GitHub / GitLab / Gerrit sections) override ``onTest`` to call
  // ``testGitForgeToken({provider, token, url})`` instead — that
  // endpoint accepts the candidate in the request body and never
  // touches ``settings``. Sections whose TEST legitimately depends
  // on saved config (Slack webhook, JIRA URL + token as a pair)
  // leave this unset and get the legacy settings-read behaviour.
  onTest?: () => Promise<TestResult>
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
      const result = onTest
        ? await onTest()
        : await api.testIntegration(integration)
      setTestResult(result)
      if (onTestResult) onTestResult(integration, result)
    } catch (e) {
      const errResult: TestResult = { status: "error", message: String(e) }
      setTestResult(errResult)
      if (onTestResult) onTestResult(integration, errResult)
    } finally {
      setTesting(false)
    }
  }, [integration, onTest, onTestResult])

  return (
    <div className="border border-[var(--border)] rounded-md overflow-hidden">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-2 px-3 py-2 bg-[var(--secondary)] hover:bg-[var(--secondary)]/80 transition-colors"
      >
        {typeof status === "boolean" && (
          <span
            className={`inline-block w-1.5 h-1.5 rounded-full shrink-0 ${
              status
                ? "bg-[var(--validation-emerald)]"
                : "bg-[var(--muted-foreground)]/30"
            }`}
            title={status ? "configured" : "not configured"}
            data-testid={statusTestId}
          />
        )}
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
      {/* B14 Part E row 245 — always-visible result strip pinned directly
          under the header's TEST button so the outcome (✅ Connected /
          ❌ error message) stays adjacent to the trigger even when the
          section body is collapsed. Replaces the prior layout that
          buried the message at the bottom of the expanded form. */}
      {(testResult || testing) && (
        <div
          data-testid={integration ? `integration-test-result-${integration}` : undefined}
          data-status={testing ? "testing" : testResult?.status}
          className={`font-mono text-[9px] px-3 py-1.5 border-t border-[var(--border)] flex items-start gap-1.5 ${
            testing ? "bg-[var(--secondary)] text-[var(--muted-foreground)]"
            : testResult?.status === "ok" ? "bg-[var(--validation-emerald)]/10 text-[var(--validation-emerald)]"
            : testResult?.status === "not_configured" ? "bg-[var(--secondary)] text-[var(--muted-foreground)]"
            : "bg-[var(--critical-red)]/10 text-[var(--critical-red)]"
          }`}
        >
          {testing ? (
            <>
              <Loader size={10} className="mt-0.5 animate-spin flex-shrink-0" />
              <span>Testing…</span>
            </>
          ) : testResult?.status === "ok" ? (
            <>
              <Check size={10} className="mt-0.5 flex-shrink-0" />
              {/* user / version / scopes are open-ended metadata on the
                  integration probe response (unknown-typed); coerce to
                  string before rendering so React's ReactNode contract
                  stays satisfied. `scopes` lands here from the GitHub probe
                  (B14 Part E row 240) so the operator can confirm the
                  token carries the expected OAuth privileges. */}
              <span>
                Connected
                {testResult.user ? ` (${String(testResult.user)})` : null}
                {testResult.version ? ` — ${String(testResult.version)}` : null}
                {testResult.scopes ? ` [scopes: ${String(testResult.scopes)}]` : null}
              </span>
            </>
          ) : testResult?.status === "not_configured" ? (
            <>
              <WifiOff size={10} className="mt-0.5 flex-shrink-0" />
              <span>{testResult.message || "Not configured"}</span>
            </>
          ) : testResult ? (
            <>
              <AlertTriangle size={10} className="mt-0.5 flex-shrink-0" />
              <span>{testResult.message || testResult.status || "Connection failed"}</span>
            </>
          ) : null}
        </div>
      )}
      {expanded && (
        <div className="px-3 py-2 space-y-1.5">
          {children}
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

// ─── Phase 5-9 (#multi-account-forge) — AccountManagerSection ─────────────
//
// Replaces the legacy `MultipleInstancesSection` (deleted in Phase 5-9).
// The legacy `github_token_map` / `gitlab_token_map` JSON blobs surface
// here as a deprecation banner only; once lifespan startup runs
// `migrate_legacy_credentials_once` they appear as `ga-legacy-*`
// `git_accounts` rows and the banner disappears. Each row in `git_accounts`
// surfaces here with platform icon + label + username + host + url_patterns
// + default ⭐ + last test status. New entries support all four platforms
// (github / gitlab / gerrit / jira) via a platform tab. Token input has a
// "TEST BEFORE SAVE" affordance: for an existing account it calls
// `POST /git-accounts/{id}/test` (probes the saved token); for a new
// candidate it calls `testGitForgeToken` (probes the body without
// persisting). Default toggle is one-click — backend's partial unique
// index serialises the cross-account demotion atomically. Delete shows a
// confirm dialog and surfaces a "this account has url_patterns — repos
// may resolve to it" warning before the destructive call.

const PLATFORMS: Array<{
  id: api.GitAccountPlatform
  label: string
  color: string
  hint: string
}> = [
  { id: "github", label: "GitHub", color: "var(--neural-blue)", hint: "github.com or GitHub Enterprise" },
  { id: "gitlab", label: "GitLab", color: "var(--hardware-orange)", hint: "gitlab.com or self-hosted GitLab" },
  { id: "gerrit", label: "Gerrit", color: "var(--validation-emerald)", hint: "Gerrit Code Review (SSH)" },
  { id: "jira", label: "JIRA", color: "var(--neural-purple)", hint: "Atlassian JIRA Cloud / Server" },
]

interface NewAccountForm {
  label: string
  instance_url: string
  username: string
  token: string
  ssh_key: string
  ssh_host: string
  ssh_port: string
  project: string
  webhook_secret: string
  url_patterns: string  // newline-separated; converted to string[] on submit
  is_default: boolean
}

const EMPTY_NEW_FORM: NewAccountForm = {
  label: "",
  instance_url: "",
  username: "",
  token: "",
  ssh_key: "",
  ssh_host: "",
  ssh_port: "",
  project: "",
  webhook_secret: "",
  url_patterns: "",
  is_default: false,
}

function parsePatterns(raw: string): string[] {
  return raw
    .split(/\r?\n|,/)
    .map(s => s.trim())
    .filter(Boolean)
}

function platformMeta(p: api.GitAccountPlatform) {
  return PLATFORMS.find(x => x.id === p) ?? PLATFORMS[0]
}

function AccountManagerSection({
  legacyGithubMap,
  legacyGitlabMap,
}: {
  legacyGithubMap: string
  legacyGitlabMap: string
}) {
  const [accounts, setAccounts] = useState<api.GitAccount[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [activePlatform, setActivePlatform] = useState<api.GitAccountPlatform>("github")
  const [adding, setAdding] = useState(false)
  const [form, setForm] = useState<NewAccountForm>(EMPTY_NEW_FORM)
  const [submitting, setSubmitting] = useState(false)
  const [pendingTest, setPendingTest] = useState(false)
  const [pendingTestResult, setPendingTestResult] = useState<TestResult | null>(null)
  const [perAccountTest, setPerAccountTest] = useState<Record<string, TestResult & { running?: boolean }>>({})
  const [confirmDelete, setConfirmDelete] = useState<api.GitAccount | null>(null)
  const [deleting, setDeleting] = useState(false)

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const resp = await api.listGitAccounts()
      setAccounts(resp.items)
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { refresh() }, [refresh])

  const filtered = accounts.filter(a => a.platform === activePlatform)

  const handleSubmitNew = async () => {
    setSubmitting(true)
    setError(null)
    try {
      const body: api.GitAccountCreate = {
        platform: activePlatform,
        label: form.label.trim(),
        instance_url: form.instance_url.trim(),
        username: form.username.trim(),
        token: form.token,
        ssh_key: form.ssh_key,
        ssh_host: form.ssh_host.trim(),
        ssh_port: form.ssh_port ? Number(form.ssh_port) : 0,
        project: form.project.trim(),
        webhook_secret: form.webhook_secret,
        url_patterns: parsePatterns(form.url_patterns),
        is_default: form.is_default,
      }
      await api.createGitAccount(body)
      setAdding(false)
      setForm(EMPTY_NEW_FORM)
      setPendingTestResult(null)
      await refresh()
    } catch (e) {
      setError(String(e))
    } finally {
      setSubmitting(false)
    }
  }

  // Probe the *unsaved* candidate via the existing `testGitForgeToken`
  // endpoint (which accepts the body without touching settings).
  // JIRA is not covered by `testGitForgeToken` — for JIRA candidates we
  // surface a hint that the operator must save and use the per-row TEST
  // (which calls `POST /git-accounts/{id}/test`).
  const handleTestPending = async () => {
    setPendingTest(true)
    setPendingTestResult(null)
    try {
      if (activePlatform === "jira") {
        setPendingTestResult({
          status: "not_configured",
          message: "JIRA candidates can be probed only after SAVE — use the per-row TEST button.",
        })
        return
      }
      const r = await api.testGitForgeToken({
        provider: activePlatform,
        token: form.token,
        url: form.instance_url,
        ssh_host: form.ssh_host,
        ssh_port: form.ssh_port ? Number(form.ssh_port) : undefined,
      })
      setPendingTestResult({ ...r, status: r.status, message: r.message })
    } catch (e) {
      setPendingTestResult({ status: "error", message: String(e) })
    } finally {
      setPendingTest(false)
    }
  }

  const handleTestRow = async (acc: api.GitAccount) => {
    setPerAccountTest(prev => ({ ...prev, [acc.id]: { status: "testing", running: true } }))
    try {
      const r = await api.testGitAccountById(acc.id)
      setPerAccountTest(prev => ({ ...prev, [acc.id]: { ...r, running: false } as TestResult }))
    } catch (e) {
      setPerAccountTest(prev => ({
        ...prev,
        [acc.id]: { status: "error", message: String(e), running: false },
      }))
    }
  }

  const handleSetDefault = async (acc: api.GitAccount) => {
    if (acc.is_default) return
    setError(null)
    try {
      await api.updateGitAccount(acc.id, { is_default: true })
      await refresh()
    } catch (e) {
      setError(String(e))
    }
  }

  const handleConfirmDelete = async () => {
    if (!confirmDelete) return
    setDeleting(true)
    setError(null)
    try {
      await api.deleteGitAccount(confirmDelete.id, { auto_elect_new_default: true })
      setConfirmDelete(null)
      setPerAccountTest(prev => {
        const next = { ...prev }
        delete next[confirmDelete.id]
        return next
      })
      await refresh()
    } catch (e) {
      setError(String(e))
    } finally {
      setDeleting(false)
    }
  }

  const renderRow = (acc: api.GitAccount) => {
    const meta = platformMeta(acc.platform)
    const test = perAccountTest[acc.id]
    return (
      <div
        key={acc.id}
        data-testid={`git-account-row-${acc.id}`}
        className="flex items-start gap-2 p-1.5 rounded border border-[var(--border)] bg-[var(--background)]"
      >
        <span
          className="font-mono text-[8px] px-1 py-0.5 rounded uppercase shrink-0 mt-0.5"
          style={{ backgroundColor: `${meta.color}22`, color: meta.color }}
        >
          {acc.platform}
        </span>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="font-mono text-[10px] text-[var(--foreground)] truncate" title={acc.label || acc.id}>
              {acc.label || acc.id}
            </span>
            {acc.is_default && (
              <span
                title="Platform default — used as fallback when no url_pattern matches"
                className="font-mono text-[8px] px-1 py-0.5 rounded bg-[var(--validation-emerald)]/15 text-[var(--validation-emerald)]"
                data-testid={`git-account-default-badge-${acc.id}`}
              >
                ★ DEFAULT
              </span>
            )}
            {!acc.enabled && (
              <span className="font-mono text-[8px] px-1 py-0.5 rounded bg-[var(--muted-foreground)]/15 text-[var(--muted-foreground)]">
                DISABLED
              </span>
            )}
          </div>
          <div className="font-mono text-[8px] text-[var(--muted-foreground)] truncate">
            {acc.username && <span>{acc.username} · </span>}
            {acc.instance_url || acc.ssh_host || "(no host)"}
            {acc.ssh_port ? `:${acc.ssh_port}` : ""}
            {acc.token_fingerprint && <span> · token {acc.token_fingerprint}</span>}
          </div>
          {acc.url_patterns.length > 0 && (
            <div className="font-mono text-[8px] text-[var(--muted-foreground)]/80 truncate" title={acc.url_patterns.join(", ")}>
              patterns: {acc.url_patterns.join(", ")}
            </div>
          )}
          {test && (
            <div
              data-testid={`git-account-test-result-${acc.id}`}
              className={`font-mono text-[8px] mt-0.5 ${
                test.status === "ok" ? "text-[var(--validation-emerald)]"
                : test.status === "testing" ? "text-[var(--neural-blue)]"
                : "text-[var(--critical-red)]"
              }`}
            >
              {test.status === "testing" ? "TESTING…" : `${(test.status as string).toUpperCase()}: ${test.message ?? "see details"}`}
            </div>
          )}
        </div>
        {!acc.is_default && (
          <button
            onClick={() => handleSetDefault(acc)}
            title="Set as platform default"
            data-testid={`git-account-set-default-${acc.id}`}
            className="px-1.5 py-0.5 rounded font-mono text-[8px] bg-[var(--secondary)] text-[var(--muted-foreground)] hover:text-[var(--validation-emerald)] hover:bg-[var(--validation-emerald)]/10 shrink-0"
          >
            ★ SET
          </button>
        )}
        <button
          onClick={() => handleTestRow(acc)}
          disabled={test?.running}
          title="Probe this account's saved credential against the live API"
          data-testid={`git-account-test-${acc.id}`}
          className="px-1.5 py-0.5 rounded font-mono text-[8px] bg-[var(--neural-blue)]/10 text-[var(--neural-blue)] hover:bg-[var(--neural-blue)]/20 disabled:opacity-30 shrink-0"
        >
          TEST
        </button>
        <button
          onClick={() => setConfirmDelete(acc)}
          title="Delete this account"
          data-testid={`git-account-delete-${acc.id}`}
          className="p-0.5 rounded hover:bg-[var(--critical-red)]/10 shrink-0"
        >
          <Trash2 size={10} className="text-[var(--muted-foreground)] hover:text-[var(--critical-red)]" />
        </button>
      </div>
    )
  }

  const hasLegacy =
    (legacyGithubMap && legacyGithubMap !== "{}" && legacyGithubMap !== "") ||
    (legacyGitlabMap && legacyGitlabMap !== "{}" && legacyGitlabMap !== "")

  return (
    <div className="space-y-2" data-testid="account-manager-section">
      <div className="flex items-center justify-between">
        <span className="font-mono text-[10px] font-bold text-[var(--neural-blue)]">
          GIT ACCOUNTS
        </span>
        <button
          onClick={refresh}
          disabled={loading}
          className="font-mono text-[8px] text-[var(--muted-foreground)] hover:text-[var(--foreground)] disabled:opacity-30"
          title="Reload from /git-accounts"
        >
          {loading ? "…" : "REFRESH"}
        </button>
      </div>
      <div className="font-mono text-[8px] text-[var(--muted-foreground)]/80 leading-relaxed">
        Manage per-platform credentials. Each row is one account; URL patterns
        route repository operations to the matching account, with the ⭐ default
        as fallback. Plaintext tokens never leave the server — list shows
        masked fingerprints only.
      </div>

      {/* Platform tabs */}
      <div className="flex items-center gap-1 border-b border-[var(--border)] overflow-x-auto">
        {PLATFORMS.map(p => (
          <button
            key={p.id}
            data-testid={`git-account-platform-tab-${p.id}`}
            onClick={() => { setActivePlatform(p.id); setAdding(false); setPendingTestResult(null) }}
            className={`px-2 py-1 font-mono text-[9px] uppercase tracking-wider transition-colors ${
              activePlatform === p.id
                ? "text-[var(--foreground)] border-b-2"
                : "text-[var(--muted-foreground)] border-b-2 border-transparent"
            }`}
            style={activePlatform === p.id ? { borderBottomColor: p.color } : undefined}
          >
            {p.label}
            {accounts.filter(a => a.platform === p.id).length > 0 && (
              <span
                className="ml-1 px-1 py-0.5 rounded text-[7px] normal-case"
                style={{ backgroundColor: `${p.color}22`, color: p.color }}
              >
                {accounts.filter(a => a.platform === p.id).length}
              </span>
            )}
          </button>
        ))}
      </div>

      {error && (
        <div
          data-testid="account-manager-error"
          className="font-mono text-[9px] text-[var(--critical-red)] bg-[var(--critical-red)]/10 px-2 py-1 rounded"
        >
          {error}
        </div>
      )}

      {/* Account list */}
      {loading ? (
        <div className="font-mono text-[9px] text-[var(--muted-foreground)] py-2">Loading…</div>
      ) : filtered.length === 0 ? (
        <div className="font-mono text-[9px] text-[var(--muted-foreground)] opacity-60 py-1">
          No {platformMeta(activePlatform).label} accounts yet — use ADD to create one.
        </div>
      ) : (
        <div className="space-y-1">{filtered.map(renderRow)}</div>
      )}

      {/* Add form */}
      {adding ? (
        <div
          className="mt-1 p-2 rounded border space-y-1.5"
          style={{ borderColor: `${platformMeta(activePlatform).color}55` }}
          data-testid="git-account-add-form"
        >
          <div className="font-mono text-[8px] uppercase tracking-wider" style={{ color: platformMeta(activePlatform).color }}>
            Add {platformMeta(activePlatform).label} account · {platformMeta(activePlatform).hint}
          </div>
          <SettingField label="Label" value={form.label} onChange={v => setForm(f => ({ ...f, label: v }))} />
          <SettingField label="Username" value={form.username} onChange={v => setForm(f => ({ ...f, username: v }))} />
          {activePlatform !== "gerrit" && (
            <SettingField
              label={activePlatform === "jira" ? "JIRA URL" : "Instance URL"}
              value={form.instance_url}
              onChange={v => setForm(f => ({ ...f, instance_url: v }))}
            />
          )}
          {activePlatform === "gerrit" && (
            <>
              <SettingField label="SSH Host" value={form.ssh_host} onChange={v => setForm(f => ({ ...f, ssh_host: v }))} />
              <SettingField label="SSH Port" value={form.ssh_port} onChange={v => setForm(f => ({ ...f, ssh_port: v }))} />
              <SettingField label="Project" value={form.project} onChange={v => setForm(f => ({ ...f, project: v }))} />
            </>
          )}
          <SettingField label="Token" value={form.token} type="password" onChange={v => setForm(f => ({ ...f, token: v }))} />
          {activePlatform === "gerrit" && (
            <SettingField label="SSH Key" value={form.ssh_key} type="password" onChange={v => setForm(f => ({ ...f, ssh_key: v }))} />
          )}
          {activePlatform !== "jira" && (
            <SettingField label="Webhook Secret" value={form.webhook_secret} type="password" onChange={v => setForm(f => ({ ...f, webhook_secret: v }))} />
          )}
          <div className="flex items-start gap-2">
            <label className="font-mono text-[9px] text-[var(--muted-foreground)] w-20 shrink-0 pt-1">URL Patterns</label>
            <textarea
              value={form.url_patterns}
              onChange={e => setForm(f => ({ ...f, url_patterns: e.target.value }))}
              rows={2}
              placeholder="github.com/acme-corp/*&#10;github.com/acme-corp/repo-x"
              className="flex-1 font-mono text-[10px] px-2 py-1 rounded bg-[var(--background)] border border-[var(--border)] text-[var(--foreground)] focus:outline-none focus:border-[var(--neural-blue)] resize-none"
              data-testid="git-account-form-url-patterns"
            />
          </div>
          <div className="flex items-center gap-2">
            <label className="font-mono text-[9px] text-[var(--muted-foreground)] w-20 shrink-0">Default ⭐</label>
            <button
              onClick={() => setForm(f => ({ ...f, is_default: !f.is_default }))}
              className={`px-2 py-0.5 rounded font-mono text-[9px] transition-colors ${
                form.is_default
                  ? "bg-[var(--validation-emerald)]/20 text-[var(--validation-emerald)]"
                  : "bg-[var(--secondary)] text-[var(--muted-foreground)]"
              }`}
              data-testid="git-account-form-default-toggle"
            >
              {form.is_default ? "ON" : "OFF"}
            </button>
          </div>
          {pendingTestResult && (
            <div
              data-testid="git-account-form-test-result"
              className={`font-mono text-[8px] px-2 py-1 rounded ${
                pendingTestResult.status === "ok" ? "text-[var(--validation-emerald)] bg-[var(--validation-emerald)]/10"
                : pendingTestResult.status === "not_configured" ? "text-[var(--muted-foreground)] bg-[var(--secondary)]"
                : "text-[var(--critical-red)] bg-[var(--critical-red)]/10"
              }`}
            >
              {pendingTestResult.status.toUpperCase()}: {pendingTestResult.message ?? "(no message)"}
            </div>
          )}
          <div className="flex items-center justify-end gap-2">
            <button
              onClick={() => { setAdding(false); setForm(EMPTY_NEW_FORM); setPendingTestResult(null) }}
              className="px-2 py-0.5 rounded font-mono text-[9px] text-[var(--muted-foreground)] hover:bg-[var(--background)]"
              data-testid="git-account-form-cancel"
            >
              CANCEL
            </button>
            <button
              onClick={handleTestPending}
              disabled={pendingTest}
              data-testid="git-account-form-test"
              title="Probe this candidate token without persisting (uses /runtime/git-forge/test-token)"
              className="px-2 py-0.5 rounded font-mono text-[9px] bg-[var(--neural-blue)]/15 text-[var(--neural-blue)] hover:bg-[var(--neural-blue)]/25 disabled:opacity-30"
            >
              {pendingTest ? "TESTING…" : "TEST BEFORE SAVE"}
            </button>
            <button
              onClick={handleSubmitNew}
              disabled={submitting}
              data-testid="git-account-form-save"
              className="px-2 py-0.5 rounded font-mono text-[9px] bg-[var(--neural-blue)] text-black font-semibold disabled:opacity-30"
            >
              {submitting ? "SAVING…" : "SAVE"}
            </button>
          </div>
        </div>
      ) : (
        <button
          onClick={() => { setAdding(true); setPendingTestResult(null) }}
          data-testid="git-account-add-button"
          className="mt-1 flex items-center gap-1 px-2 py-1 rounded font-mono text-[9px] hover:bg-[var(--background)] transition-colors"
          style={{ color: platformMeta(activePlatform).color }}
        >
          <Plus size={10} /> Add {platformMeta(activePlatform).label} Account
        </button>
      )}

      {/* Delete confirm */}
      {confirmDelete && (
        <div
          data-testid="git-account-delete-confirm"
          className="mt-1 p-2 rounded border border-[var(--critical-red)]/40 bg-[var(--critical-red)]/5 space-y-1.5"
        >
          <div className="font-mono text-[9px] text-[var(--critical-red)] uppercase tracking-wider">
            Delete {confirmDelete.label || confirmDelete.id}?
          </div>
          {confirmDelete.url_patterns.length > 0 && (
            <div className="font-mono text-[8px] text-[var(--hardware-orange)]">
              ⚠ This account has {confirmDelete.url_patterns.length} URL pattern(s) — repos matching{" "}
              <code>{confirmDelete.url_patterns.join(", ")}</code>{" "}
              may be left without a credential.
            </div>
          )}
          {confirmDelete.is_default && (
            <div className="font-mono text-[8px] text-[var(--hardware-orange)]">
              ⚠ This is the platform default. Backend will auto-elect the next
              available {confirmDelete.platform} account if any exists.
            </div>
          )}
          <div className="flex items-center justify-end gap-2">
            <button
              onClick={() => setConfirmDelete(null)}
              className="px-2 py-0.5 rounded font-mono text-[9px] text-[var(--muted-foreground)] hover:bg-[var(--background)]"
              data-testid="git-account-delete-cancel"
            >
              CANCEL
            </button>
            <button
              onClick={handleConfirmDelete}
              disabled={deleting}
              data-testid="git-account-delete-confirm-button"
              className="px-2 py-0.5 rounded font-mono text-[9px] bg-[var(--critical-red)] text-black font-semibold disabled:opacity-30"
            >
              {deleting ? "DELETING…" : "DELETE"}
            </button>
          </div>
        </div>
      )}

      {/* Legacy token-map deprecation banner */}
      {hasLegacy && (
        <div
          data-testid="account-manager-legacy-banner"
          className="mt-2 p-2 rounded border border-[var(--hardware-orange)]/30 bg-[var(--hardware-orange)]/5 space-y-1"
        >
          <div className="font-mono text-[8px] text-[var(--hardware-orange)] uppercase tracking-wider">
            Legacy (will auto-migrate on next login)
          </div>
          {legacyGithubMap && legacyGithubMap !== "{}" && legacyGithubMap !== "" && (
            <div className="font-mono text-[8px] text-[var(--muted-foreground)] break-all">
              github_token_map: <code>{legacyGithubMap}</code>
            </div>
          )}
          {legacyGitlabMap && legacyGitlabMap !== "{}" && legacyGitlabMap !== "" && (
            <div className="font-mono text-[8px] text-[var(--muted-foreground)] break-all">
              gitlab_token_map: <code>{legacyGitlabMap}</code>
            </div>
          )}
          <div className="font-mono text-[7px] text-[var(--muted-foreground)]/70 leading-relaxed">
            These map entries persist in the legacy
            OMNISIGHT_*_TOKEN_MAP settings until backend lifespan
            startup runs `migrate_legacy_credentials_once`. After
            migration, the rows above appear as `ga-legacy-*` accounts
            and this banner disappears.
          </div>
        </div>
      )}
    </div>
  )
}

// ─── Phase 5b-4 (#llm-credentials) — LLMCredentialManagerSection ───────────
//
// Replaces the legacy single-field-per-provider LLM PROVIDERS grid (8 password
// inputs saved via `PUT /runtime/settings`) with a per-provider, multi-account,
// DB-backed manager that wraps the `/api/v1/llm-credentials` CRUD surface
// (Phase 5b-3). Each provider tab lists its credentials with masked
// fingerprints; per-row TEST probes the stored key live (POST {id}/test);
// per-row ROTATE swaps the key in-place (PATCH {id} {value}); per-row SET
// promotes to platform default (PATCH {id} {is_default: true}); DELETE shows a
// confirm dialog with an auto-elect-new-default note. ADD supports the same
// metadata.base_url for Ollama and plain label+value for the 8 keyed providers.
//
// Why no "TEST BEFORE SAVE" affordance: unlike git-forge credentials, the
// backend does not expose an unsaved-candidate probe endpoint for LLMs. The
// Phase 5b-3 test surface is strictly saved-credential-id scoped. Operators
// SAVE first, then use the per-row TEST button.

const LLM_PROVIDERS_META: Array<{
  id: api.LLMCredentialProvider
  label: string
  color: string
  hint: string
  keyless: boolean
}> = [
  { id: "anthropic", label: "Anthropic", color: "var(--neural-blue)", hint: "Claude — x-api-key header auth", keyless: false },
  { id: "openai", label: "OpenAI", color: "var(--validation-emerald)", hint: "GPT — Bearer auth", keyless: false },
  { id: "google", label: "Google", color: "var(--hardware-orange)", hint: "Gemini — query-param key auth", keyless: false },
  { id: "openrouter", label: "OpenRouter", color: "var(--neural-purple)", hint: "Aggregator — Bearer auth", keyless: false },
  { id: "xai", label: "xAI (Grok)", color: "var(--critical-red)", hint: "Grok — Bearer auth", keyless: false },
  { id: "groq", label: "Groq", color: "var(--neural-blue)", hint: "Groq LPU — Bearer auth", keyless: false },
  { id: "deepseek", label: "DeepSeek", color: "var(--validation-emerald)", hint: "DeepSeek — Bearer auth", keyless: false },
  { id: "together", label: "Together", color: "var(--hardware-orange)", hint: "Together.ai — Bearer auth", keyless: false },
  { id: "ollama", label: "Ollama", color: "var(--neural-purple)", hint: "Local (no key) — needs metadata.base_url", keyless: true },
]

interface NewLlmCredentialForm {
  label: string
  value: string
  base_url: string     // ollama only
  is_default: boolean
}

const EMPTY_LLM_FORM: NewLlmCredentialForm = {
  label: "",
  value: "",
  base_url: "",
  is_default: false,
}

function llmProviderMeta(p: api.LLMCredentialProvider) {
  return LLM_PROVIDERS_META.find(x => x.id === p) ?? LLM_PROVIDERS_META[0]
}

function LLMCredentialManagerSection() {
  const [creds, setCreds] = useState<api.LLMCredential[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [activeProvider, setActiveProvider] = useState<api.LLMCredentialProvider>("anthropic")
  const [adding, setAdding] = useState(false)
  const [form, setForm] = useState<NewLlmCredentialForm>(EMPTY_LLM_FORM)
  const [submitting, setSubmitting] = useState(false)
  const [perRowTest, setPerRowTest] = useState<Record<string, TestResult & { running?: boolean }>>({})
  const [rotating, setRotating] = useState<api.LLMCredential | null>(null)
  const [rotateValue, setRotateValue] = useState("")
  const [rotateSubmitting, setRotateSubmitting] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState<api.LLMCredential | null>(null)
  const [deleting, setDeleting] = useState(false)

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const resp = await api.listLlmCredentials()
      setCreds(resp.items)
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { refresh() }, [refresh])

  const filtered = creds.filter(c => c.provider === activeProvider)

  const handleSubmitNew = async () => {
    setSubmitting(true)
    setError(null)
    try {
      const meta = llmProviderMeta(activeProvider)
      const body: api.LLMCredentialCreate = {
        provider: activeProvider,
        label: form.label.trim(),
        value: meta.keyless ? "" : form.value,
        is_default: form.is_default,
        metadata: meta.keyless && form.base_url.trim()
          ? { base_url: form.base_url.trim() }
          : {},
      }
      await api.createLlmCredential(body)
      setAdding(false)
      setForm(EMPTY_LLM_FORM)
      await refresh()
    } catch (e) {
      setError(String(e))
    } finally {
      setSubmitting(false)
    }
  }

  const handleTestRow = async (c: api.LLMCredential) => {
    setPerRowTest(prev => ({ ...prev, [c.id]: { status: "testing", running: true } }))
    try {
      const r = await api.testLlmCredentialById(c.id)
      setPerRowTest(prev => ({ ...prev, [c.id]: { ...r, running: false } as TestResult }))
    } catch (e) {
      setPerRowTest(prev => ({
        ...prev,
        [c.id]: { status: "error", message: String(e), running: false },
      }))
    }
  }

  const handleSetDefault = async (c: api.LLMCredential) => {
    if (c.is_default) return
    setError(null)
    try {
      await api.updateLlmCredential(c.id, { is_default: true })
      await refresh()
    } catch (e) {
      setError(String(e))
    }
  }

  const handleConfirmRotate = async () => {
    if (!rotating) return
    setRotateSubmitting(true)
    setError(null)
    try {
      await api.updateLlmCredential(rotating.id, { value: rotateValue })
      setRotating(null)
      setRotateValue("")
      await refresh()
    } catch (e) {
      setError(String(e))
    } finally {
      setRotateSubmitting(false)
    }
  }

  const handleConfirmDelete = async () => {
    if (!confirmDelete) return
    setDeleting(true)
    setError(null)
    try {
      await api.deleteLlmCredential(confirmDelete.id, { auto_elect_new_default: true })
      setConfirmDelete(null)
      setPerRowTest(prev => {
        const next = { ...prev }
        delete next[confirmDelete.id]
        return next
      })
      await refresh()
    } catch (e) {
      setError(String(e))
    } finally {
      setDeleting(false)
    }
  }

  const renderRow = (c: api.LLMCredential) => {
    const meta = llmProviderMeta(c.provider)
    const test = perRowTest[c.id]
    const baseUrl = typeof c.metadata?.base_url === "string" ? c.metadata.base_url as string : ""
    return (
      <div
        key={c.id}
        data-testid={`llm-credential-row-${c.id}`}
        className="flex items-start gap-2 p-1.5 rounded border border-[var(--border)] bg-[var(--background)]"
      >
        <span
          className="font-mono text-[8px] px-1 py-0.5 rounded uppercase shrink-0 mt-0.5"
          style={{ backgroundColor: `${meta.color}22`, color: meta.color }}
        >
          {c.provider}
        </span>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="font-mono text-[10px] text-[var(--foreground)] truncate" title={c.label || c.id}>
              {c.label || c.id}
            </span>
            {c.is_default && (
              <span
                title="Provider default — resolver picks this when no explicit credential is requested"
                className="font-mono text-[8px] px-1 py-0.5 rounded bg-[var(--validation-emerald)]/15 text-[var(--validation-emerald)]"
                data-testid={`llm-credential-default-badge-${c.id}`}
              >
                ★ DEFAULT
              </span>
            )}
            {!c.enabled && (
              <span className="font-mono text-[8px] px-1 py-0.5 rounded bg-[var(--muted-foreground)]/15 text-[var(--muted-foreground)]">
                DISABLED
              </span>
            )}
          </div>
          <div className="font-mono text-[8px] text-[var(--muted-foreground)] truncate">
            {c.value_fingerprint
              ? <span>key {c.value_fingerprint}</span>
              : meta.keyless
                ? <span>keyless · {baseUrl || "(no base_url)"}</span>
                : <span>(no key)</span>}
          </div>
          {test && (
            <div
              data-testid={`llm-credential-test-result-${c.id}`}
              className={`font-mono text-[8px] mt-0.5 ${
                test.status === "ok" ? "text-[var(--validation-emerald)]"
                : test.status === "testing" ? "text-[var(--neural-blue)]"
                : "text-[var(--critical-red)]"
              }`}
            >
              {test.status === "testing" ? "TESTING…" : `${(test.status as string).toUpperCase()}: ${test.message ?? (test.model_count !== undefined ? `${test.model_count} models` : "ok")}`}
            </div>
          )}
        </div>
        {!c.is_default && (
          <button
            onClick={() => handleSetDefault(c)}
            title="Set as provider default"
            data-testid={`llm-credential-set-default-${c.id}`}
            className="px-1.5 py-0.5 rounded font-mono text-[8px] bg-[var(--secondary)] text-[var(--muted-foreground)] hover:text-[var(--validation-emerald)] hover:bg-[var(--validation-emerald)]/10 shrink-0"
          >
            ★ SET
          </button>
        )}
        <button
          onClick={() => handleTestRow(c)}
          disabled={test?.running}
          title="Probe this credential's stored key against the provider's list-models endpoint"
          data-testid={`llm-credential-test-${c.id}`}
          className="px-1.5 py-0.5 rounded font-mono text-[8px] bg-[var(--neural-blue)]/10 text-[var(--neural-blue)] hover:bg-[var(--neural-blue)]/20 disabled:opacity-30 shrink-0"
        >
          TEST
        </button>
        {!meta.keyless && (
          <button
            onClick={() => { setRotating(c); setRotateValue("") }}
            title="Rotate the API key in place (old ciphertext replaced, version bumped)"
            data-testid={`llm-credential-rotate-${c.id}`}
            className="px-1.5 py-0.5 rounded font-mono text-[8px] bg-[var(--hardware-orange)]/10 text-[var(--hardware-orange)] hover:bg-[var(--hardware-orange)]/20 shrink-0"
          >
            ROTATE
          </button>
        )}
        <button
          onClick={() => setConfirmDelete(c)}
          title="Delete this credential"
          data-testid={`llm-credential-delete-${c.id}`}
          className="p-0.5 rounded hover:bg-[var(--critical-red)]/10 shrink-0"
        >
          <Trash2 size={10} className="text-[var(--muted-foreground)] hover:text-[var(--critical-red)]" />
        </button>
      </div>
    )
  }

  const activeMeta = llmProviderMeta(activeProvider)

  return (
    <div className="space-y-2" data-testid="llm-credential-manager-section">
      <div className="flex items-center justify-between">
        <span className="font-mono text-[10px] font-bold text-[var(--neural-blue)]">
          LLM CREDENTIALS
        </span>
        <button
          onClick={refresh}
          disabled={loading}
          className="font-mono text-[8px] text-[var(--muted-foreground)] hover:text-[var(--foreground)] disabled:opacity-30"
          title="Reload from /llm-credentials"
        >
          {loading ? "…" : "REFRESH"}
        </button>
      </div>
      <div className="font-mono text-[8px] text-[var(--muted-foreground)]/80 leading-relaxed">
        Multi-account LLM provider keys, Fernet-encrypted at rest. Resolver picks
        the ⭐ default per provider unless explicitly overridden. Plaintext never
        leaves the server — list shows masked fingerprints only. Changes persist
        across backend restarts.
      </div>

      {/* Provider tabs */}
      <div className="flex items-center gap-1 border-b border-[var(--border)] overflow-x-auto">
        {LLM_PROVIDERS_META.map(p => (
          <button
            key={p.id}
            data-testid={`llm-credential-provider-tab-${p.id}`}
            onClick={() => { setActiveProvider(p.id); setAdding(false) }}
            className={`px-2 py-1 font-mono text-[9px] uppercase tracking-wider transition-colors whitespace-nowrap ${
              activeProvider === p.id
                ? "text-[var(--foreground)] border-b-2"
                : "text-[var(--muted-foreground)] border-b-2 border-transparent"
            }`}
            style={activeProvider === p.id ? { borderBottomColor: p.color } : undefined}
          >
            {p.label}
            {creds.filter(c => c.provider === p.id).length > 0 && (
              <span
                className="ml-1 px-1 py-0.5 rounded text-[7px] normal-case"
                style={{ backgroundColor: `${p.color}22`, color: p.color }}
              >
                {creds.filter(c => c.provider === p.id).length}
              </span>
            )}
          </button>
        ))}
      </div>

      {error && (
        <div
          data-testid="llm-credential-manager-error"
          className="font-mono text-[9px] text-[var(--critical-red)] bg-[var(--critical-red)]/10 px-2 py-1 rounded"
        >
          {error}
        </div>
      )}

      {/* Credential list */}
      {loading ? (
        <div className="font-mono text-[9px] text-[var(--muted-foreground)] py-2">Loading…</div>
      ) : filtered.length === 0 ? (
        <div className="font-mono text-[9px] text-[var(--muted-foreground)] opacity-60 py-1">
          No {activeMeta.label} credentials yet — use ADD to create one.
        </div>
      ) : (
        <div className="space-y-1">{filtered.map(renderRow)}</div>
      )}

      {/* Add form */}
      {adding ? (
        <div
          className="mt-1 p-2 rounded border space-y-1.5"
          style={{ borderColor: `${activeMeta.color}55` }}
          data-testid="llm-credential-add-form"
        >
          <div className="font-mono text-[8px] uppercase tracking-wider" style={{ color: activeMeta.color }}>
            Add {activeMeta.label} credential · {activeMeta.hint}
          </div>
          <SettingField label="Label" value={form.label} onChange={v => setForm(f => ({ ...f, label: v }))} />
          {activeMeta.keyless ? (
            <SettingField label="Base URL" value={form.base_url} onChange={v => setForm(f => ({ ...f, base_url: v }))} />
          ) : (
            <SettingField label="API Key" value={form.value} type="password" onChange={v => setForm(f => ({ ...f, value: v }))} />
          )}
          <div className="flex items-center gap-2">
            <label className="font-mono text-[9px] text-[var(--muted-foreground)] w-20 shrink-0">Default ⭐</label>
            <button
              onClick={() => setForm(f => ({ ...f, is_default: !f.is_default }))}
              className={`px-2 py-0.5 rounded font-mono text-[9px] transition-colors ${
                form.is_default
                  ? "bg-[var(--validation-emerald)]/20 text-[var(--validation-emerald)]"
                  : "bg-[var(--secondary)] text-[var(--muted-foreground)]"
              }`}
              data-testid="llm-credential-form-default-toggle"
            >
              {form.is_default ? "ON" : "OFF"}
            </button>
          </div>
          <div className="flex items-center justify-end gap-2">
            <button
              onClick={() => { setAdding(false); setForm(EMPTY_LLM_FORM) }}
              className="px-2 py-0.5 rounded font-mono text-[9px] text-[var(--muted-foreground)] hover:bg-[var(--background)]"
              data-testid="llm-credential-form-cancel"
            >
              CANCEL
            </button>
            <button
              onClick={handleSubmitNew}
              disabled={submitting}
              data-testid="llm-credential-form-save"
              className="px-2 py-0.5 rounded font-mono text-[9px] bg-[var(--neural-blue)] text-black font-semibold disabled:opacity-30"
            >
              {submitting ? "SAVING…" : "SAVE"}
            </button>
          </div>
        </div>
      ) : (
        <button
          onClick={() => setAdding(true)}
          data-testid="llm-credential-add-button"
          className="mt-1 flex items-center gap-1 px-2 py-1 rounded font-mono text-[9px] hover:bg-[var(--background)] transition-colors"
          style={{ color: activeMeta.color }}
        >
          <Plus size={10} /> Add {activeMeta.label} Credential
        </button>
      )}

      {/* Rotate dialog */}
      {rotating && (
        <div
          data-testid="llm-credential-rotate-dialog"
          className="mt-1 p-2 rounded border border-[var(--hardware-orange)]/40 bg-[var(--hardware-orange)]/5 space-y-1.5"
        >
          <div className="font-mono text-[9px] text-[var(--hardware-orange)] uppercase tracking-wider">
            Rotate {rotating.label || rotating.id} · {rotating.provider}
          </div>
          <div className="font-mono text-[8px] text-[var(--muted-foreground)]">
            Current key {rotating.value_fingerprint || "(unset)"}. Paste the new key
            below — old ciphertext is replaced on PATCH and the version counter bumps.
          </div>
          <SettingField
            label="New Key"
            value={rotateValue}
            type="password"
            onChange={v => setRotateValue(v)}
          />
          <div className="flex items-center justify-end gap-2">
            <button
              onClick={() => { setRotating(null); setRotateValue("") }}
              className="px-2 py-0.5 rounded font-mono text-[9px] text-[var(--muted-foreground)] hover:bg-[var(--background)]"
              data-testid="llm-credential-rotate-cancel"
            >
              CANCEL
            </button>
            <button
              onClick={handleConfirmRotate}
              disabled={rotateSubmitting || rotateValue.length === 0}
              data-testid="llm-credential-rotate-confirm"
              className="px-2 py-0.5 rounded font-mono text-[9px] bg-[var(--hardware-orange)] text-black font-semibold disabled:opacity-30"
            >
              {rotateSubmitting ? "ROTATING…" : "ROTATE"}
            </button>
          </div>
        </div>
      )}

      {/* Delete confirm */}
      {confirmDelete && (
        <div
          data-testid="llm-credential-delete-confirm"
          className="mt-1 p-2 rounded border border-[var(--critical-red)]/40 bg-[var(--critical-red)]/5 space-y-1.5"
        >
          <div className="font-mono text-[9px] text-[var(--critical-red)] uppercase tracking-wider">
            Delete {confirmDelete.label || confirmDelete.id}?
          </div>
          {confirmDelete.is_default && (
            <div className="font-mono text-[8px] text-[var(--hardware-orange)]">
              ⚠ This is the provider default. Backend will auto-elect the next
              available {confirmDelete.provider} credential if any exists.
            </div>
          )}
          <div className="flex items-center justify-end gap-2">
            <button
              onClick={() => setConfirmDelete(null)}
              className="px-2 py-0.5 rounded font-mono text-[9px] text-[var(--muted-foreground)] hover:bg-[var(--background)]"
              data-testid="llm-credential-delete-cancel"
            >
              CANCEL
            </button>
            <button
              onClick={handleConfirmDelete}
              disabled={deleting}
              data-testid="llm-credential-delete-confirm-button"
              className="px-2 py-0.5 rounded font-mono text-[9px] bg-[var(--critical-red)] text-black font-semibold disabled:opacity-30"
            >
              {deleting ? "DELETING…" : "DELETE"}
            </button>
          </div>
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
  // FX.2.4: guard against setState after unmount when an in-flight
  // refresh resolves after clearInterval has run. Ref keeps the
  // refresh callback identity stable (empty deps) — the interval
  // never re-installs on re-render.
  const mountedRef = useRef(true)

  const refresh = useCallback(async () => {
    try {
      const r = await api.getCircuitBreakers("tenant")
      if (!mountedRef.current) return
      setError(null)
      setData(r)
    } catch (e) {
      if (!mountedRef.current) return
      setError(String(e))
    }
  }, [])

  useEffect(() => {
    mountedRef.current = true
    refresh()
    // M3: cooldown ticks every second; refresh every 10s so stale state
    // doesn't linger after a key recovers without manual reload.
    const id = setInterval(refresh, 10_000)
    return () => {
      mountedRef.current = false
      clearInterval(id)
    }
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

// B14 Part D row 236 — three-state banner rendered at the TOP of every
// Integration Settings tab. This is a distinct visual element from the
// tiny 1.5px dots inside each TabsTrigger (those show a passive
// "configured / not configured" hint inside the tab header). The banner
// lives inside the tab body so it's unambiguous which integration family
// the status refers to, and it surfaces a third `error` state that the
// trigger dot can't express because it only gets 1.5px of pixel budget.
//
// Semantics:
//   - `connected`      : at least one field in the tab is populated AND
//                        no recorded probe (TEST button) has failed.
//   - `not_configured` : nothing populated (and the user may not realise
//                        yet — e.g. first-run opens the Git tab empty).
//   - `error`          : a recent probe returned status !== "ok", which
//                        is a stronger signal than "configured" alone
//                        and therefore takes priority in the state
//                        machine.
type TabConnectionStatus = "connected" | "not_configured" | "error"

const TAB_STATUS_CONFIG: Record<TabConnectionStatus, {
  label: string
  icon: React.ReactNode
  // Tailwind/var() fragment — inlined here (vs. a `variants` util) so the
  // one-off design-system tokens (validation-emerald / hardware-orange /
  // critical-red) stay obvious during theme audits.
  cls: string
}> = {
  connected: {
    label: "CONNECTED",
    icon: <Check size={12} />,
    cls: "border-[var(--validation-emerald)]/40 bg-[var(--validation-emerald)]/10 text-[var(--validation-emerald)]",
  },
  not_configured: {
    label: "NOT CONFIGURED",
    icon: <AlertTriangle size={12} />,
    cls: "border-[var(--hardware-orange)]/40 bg-[var(--hardware-orange)]/10 text-[var(--hardware-orange)]",
  },
  error: {
    label: "CONNECTION ERROR",
    icon: <X size={12} />,
    cls: "border-[var(--critical-red)]/40 bg-[var(--critical-red)]/10 text-[var(--critical-red)]",
  },
}

function TabStatusBadge({ status, testId, message }: {
  status: TabConnectionStatus
  testId: string
  // When `status === "error"`, we surface the upstream probe message so
  // the operator can tell WHY it failed without having to expand the
  // individual section. Ignored for the other two states since they are
  // self-explanatory.
  message?: string
}) {
  const cfg = TAB_STATUS_CONFIG[status]
  return (
    <div
      className={`flex items-center gap-2 px-2.5 py-1.5 rounded-md border font-mono text-[9px] tracking-fui ${cfg.cls}`}
      data-testid={testId}
      data-status={status}
      role="status"
    >
      <span className="shrink-0">{cfg.icon}</span>
      <span className="font-semibold">{cfg.label}</span>
      {status === "error" && message && (
        <span
          className="flex-1 truncate font-normal opacity-80"
          title={message}
          data-testid={`${testId}-message`}
        >
          — {message}
        </span>
      )}
    </div>
  )
}

// Which backend integration probes belong to which Integration Settings
// tab. The mapping is load-bearing: when a section's TEST button fires,
// we use this table to route the result into the correct tab badge. CI/CD
// has no entries today because the CI/CD sections don't expose a probe —
// the CI trigger only fires during actual pipeline dispatches, not as a
// standalone health check — so the CI/CD badge is driven purely by the
// passive "configured" signal from `tabStatus.cicd`.
const TAB_INTEGRATIONS: Record<"git" | "gerrit" | "webhooks" | "cicd", readonly string[]> = {
  // B14 Part E row 240: each Git forge owns its own probe so the tab badge
  // flips red whenever ANY forge probe fails (a stale GitHub token
  // shouldn't be hidden by a working SSH key). The Git tab now dispatches
  // three independent SettingsSections (SSH / GitHub / GitLab); each
  // surfaces its own TEST button and routes the result back here.
  git: ["ssh", "github", "gitlab"],
  gerrit: ["gerrit"],
  webhooks: ["jira", "slack"],
  cicd: [],
}

// 2026-04-22: operator reported that pressing SAVE & APPLY
// "seems to do nothing" after entering a Google API key. Root cause:
//   * Save did succeed at the backend (``PUT /runtime/settings`` is
//     wired + the value lands on ``settings.google_api_key``), but the
//     modal's local ``providers`` state was only fetched on ``open``
//     → the green "configured" dot stayed off after save so it looked
//     like nothing happened.
//   * No success / error toast meant a successful save left zero
//     visible feedback past the button flicker.
//   * Any rejected fields from the ``updates`` dict (e.g. a typo that
//     doesn't match ``_UPDATABLE_FIELDS``) were silently swallowed.
// Fix: post-save we refetch providers AND expose an inline feedback
// banner at the top of the modal that reports applied count + any
// rejected fields (with reason). Banner auto-dismisses after 4s.
//
// Known follow-up (not this fix): ``PUT /runtime/settings`` only
// mutates the in-memory ``settings`` object — values reset on the
// next backend restart. Persisting to ``.env`` / DB is a separate
// design decision (secret_store for encryption, tenant scoping,
// audit). Tracked in Phase 5 adjacent discussion (``git_accounts``
// uses the same secret_store model).
type SaveFeedback =
  | { kind: "ok"; applied: string[]; rejected: Record<string, string> }
  | { kind: "error"; message: string }
  | null

// ─── Y-prep.2 #288 — JIRA webhook secret rotation dialog ───
//
// Aligned with the Gerrit Wizard Step 5 one-time-reveal UX pattern:
// confirm → rotate API call → one-time modal showing the plain secret
// with copy-to-clipboard → close drops the plain value and only the
// masked preview survives.
//
// The rotate endpoint (`POST /runtime/git-forge/jira/webhook-secret/generate`)
// returns the plain `secret` exactly once; if the operator dismisses
// this dialog without copying, the secret is unrecoverable and must
// be re-rotated. That's why both phases force a deliberate click —
// no backdrop-click auto-close while the plain value is on screen.
function JiraWebhookSecretRotateDialog({
  open,
  onClose,
  onRotated,
}: {
  open: boolean
  onClose: () => void
  onRotated: (maskedPreview: string) => void
}) {
  const [phase, setPhase] = useState<"confirm" | "rotating" | "revealed" | "error">("confirm")
  const [plainSecret, setPlainSecret] = useState("")
  const [maskedSecret, setMaskedSecret] = useState("")
  const [webhookUrl, setWebhookUrl] = useState("")
  const [signatureHeader, setSignatureHeader] = useState("")
  const [note, setNote] = useState("")
  const [errorMsg, setErrorMsg] = useState("")
  const [secretCopied, setSecretCopied] = useState(false)

  useEffect(() => {
    if (open) {
      setPhase("confirm")
      setPlainSecret("")
      setMaskedSecret("")
      setWebhookUrl("")
      setSignatureHeader("")
      setNote("")
      setErrorMsg("")
      setSecretCopied(false)
    }
  }, [open])

  const onConfirmRotate = useCallback(async () => {
    setPhase("rotating")
    setErrorMsg("")
    try {
      const res = await api.generateJiraWebhookSecret()
      if (res.status === "ok" && res.secret) {
        setPlainSecret(res.secret)
        setMaskedSecret(res.secret_masked ?? "")
        setWebhookUrl(res.webhook_url ?? "")
        setSignatureHeader(res.signature_header ?? "Authorization")
        setNote(res.note ?? "")
        setPhase("revealed")
      } else {
        setErrorMsg(res.message || "Failed to rotate JIRA webhook secret")
        setPhase("error")
      }
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "Failed to rotate JIRA webhook secret")
      setPhase("error")
    }
  }, [])

  const onCopySecret = useCallback(async () => {
    if (!plainSecret) return
    try {
      await navigator.clipboard.writeText(plainSecret)
      setSecretCopied(true)
    } catch {
      /* clipboard unavailable (non-secure context) — operator can still select+copy */
    }
  }, [plainSecret])

  const onCloseRevealed = useCallback(() => {
    // Bubble the masked preview up to the parent so it can keep showing
    // the "••••abc4 (rotated)" strip after this dialog unmounts.
    if (maskedSecret) onRotated(maskedSecret)
    onClose()
  }, [maskedSecret, onRotated, onClose])

  if (!open || typeof document === "undefined") return null

  return createPortal(
    <div className="fixed inset-0 z-[120] flex items-center justify-center" data-testid="jira-webhook-rotate-dialog">
      <div
        className="absolute inset-0 bg-black/70 backdrop-blur-sm"
        // While the plain secret is on screen, a stray backdrop click
        // would nuke an unrecoverable value — disable it in the
        // "revealed" phase. Other phases keep the normal dismiss UX.
        onClick={phase === "revealed" ? undefined : onClose}
      />
      <div className="relative z-10 w-full max-w-md m-4 bg-[var(--card)] border border-[var(--border)] rounded-lg shadow-2xl flex flex-col overflow-hidden">
        <div className="flex items-center justify-between px-4 py-3 border-b border-[var(--border)] bg-[var(--secondary)]">
          <div className="flex items-center gap-2">
            <Key size={14} className="text-[var(--neural-blue)]" />
            <h2 className="font-sans text-sm font-semibold tracking-fui text-[var(--neural-blue)]">
              JIRA WEBHOOK SECRET — ROTATE
            </h2>
          </div>
          <button
            onClick={phase === "revealed" ? onCloseRevealed : onClose}
            className="p-1 rounded hover:bg-[var(--background)] transition-colors"
            aria-label="Close rotate dialog"
            data-testid="jira-webhook-rotate-close"
          >
            <X size={14} className="text-[var(--muted-foreground)]" />
          </button>
        </div>

        <div className="p-4 space-y-3">
          {phase === "confirm" && (
            <div className="space-y-3" data-testid="jira-webhook-rotate-confirm">
              <div className="flex items-start gap-2 p-2 rounded border border-[var(--hardware-orange)]/40 bg-[var(--hardware-orange)]/10 text-[var(--hardware-orange)]">
                <AlertTriangle size={12} className="mt-0.5 flex-shrink-0" />
                <div className="font-mono text-[10px] leading-relaxed">
                  旋轉將立即作廢目前的 JIRA webhook secret。舊 secret 會失效，
                  在你把新值貼進 JIRA webhook 的 <code>Authorization: Bearer …</code> header 之前，
                  inbound 事件將無法通過驗證。
                </div>
              </div>
              <div className="font-mono text-[10px] text-[var(--muted-foreground)] leading-relaxed">
                新 secret 只會顯示一次，關閉視窗後僅保留遮罩預覽。請準備好 JIRA 那邊的
                webhook 設定頁，旋轉後立刻複製貼上。
              </div>
              <div className="flex items-center justify-end gap-2 pt-1">
                <button
                  type="button"
                  onClick={onClose}
                  data-testid="jira-webhook-rotate-cancel"
                  className="px-3 py-1 rounded font-mono text-[10px] border border-[var(--border)] text-[var(--muted-foreground)] hover:bg-[var(--background)] transition-colors"
                >
                  CANCEL
                </button>
                <button
                  type="button"
                  onClick={onConfirmRotate}
                  data-testid="jira-webhook-rotate-confirm-button"
                  className="flex items-center gap-1.5 px-3 py-1 rounded font-mono text-[10px] bg-[var(--critical-red)]/15 text-[var(--critical-red)] hover:bg-[var(--critical-red)]/25 transition-colors font-semibold"
                >
                  <RefreshCw size={10} />
                  ROTATE NOW
                </button>
              </div>
            </div>
          )}

          {phase === "rotating" && (
            <div
              className="flex items-center gap-2 p-4 justify-center"
              data-testid="jira-webhook-rotate-loading"
            >
              <Loader size={14} className="animate-spin text-[var(--neural-blue)]" />
              <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
                Minting new secret…
              </span>
            </div>
          )}

          {phase === "revealed" && (
            <div className="space-y-2" data-testid="jira-webhook-rotate-revealed">
              <div className="p-2 rounded border border-[var(--validation-emerald)]/40 bg-[var(--validation-emerald)]/10 text-[var(--validation-emerald)] font-mono text-[10px] font-semibold">
                NEW SECRET — 僅顯示一次，請立即複製
              </div>
              <div className="space-y-0.5">
                <div className="font-mono text-[9px] text-[var(--muted-foreground)]">
                  Secret（貼到 JIRA webhook 的 <code>{signatureHeader || "Authorization"}: Bearer …</code>）
                </div>
                <div className="flex items-center gap-2">
                  <code
                    data-testid="jira-webhook-rotate-secret-plain"
                    className="flex-1 font-mono text-[10px] px-2 py-1 rounded bg-[var(--background)] border border-[var(--border)] text-[var(--foreground)] break-all select-all"
                  >
                    {plainSecret}
                  </code>
                  <button
                    type="button"
                    onClick={onCopySecret}
                    data-testid="jira-webhook-rotate-copy-secret"
                    className="flex items-center gap-1 px-2 py-1 rounded font-mono text-[9px] bg-[var(--validation-emerald)]/10 text-[var(--validation-emerald)] hover:bg-[var(--validation-emerald)]/20 transition-colors shrink-0"
                  >
                    <Copy size={10} />
                    {secretCopied ? "Copied" : "Copy"}
                  </button>
                </div>
              </div>
              {webhookUrl && (
                <div className="space-y-0.5 pt-1">
                  <div className="font-mono text-[9px] text-[var(--muted-foreground)]">Webhook URL</div>
                  <code
                    data-testid="jira-webhook-rotate-webhook-url"
                    className="block font-mono text-[9px] px-2 py-1 rounded bg-[var(--secondary)] text-[var(--foreground)] break-all"
                  >
                    {webhookUrl}
                  </code>
                </div>
              )}
              {note && (
                <div className="font-mono text-[9px] text-[var(--muted-foreground)] leading-relaxed pt-1">
                  {note}
                </div>
              )}
              <div className="flex items-center justify-end pt-1">
                <button
                  type="button"
                  onClick={onCloseRevealed}
                  data-testid="jira-webhook-rotate-close-revealed"
                  className="flex items-center gap-1.5 px-3 py-1 rounded font-mono text-[10px] bg-[var(--neural-blue)] text-black font-semibold hover:opacity-90 transition-colors"
                >
                  <Check size={10} />
                  SAVED & CLOSE
                </button>
              </div>
            </div>
          )}

          {phase === "error" && (
            <div className="space-y-2" data-testid="jira-webhook-rotate-error">
              <div className="flex items-start gap-2 p-2 rounded border border-[var(--critical-red)]/40 bg-[var(--critical-red)]/10 text-[var(--critical-red)]">
                <AlertTriangle size={12} className="mt-0.5 flex-shrink-0" />
                <div className="font-mono text-[10px] leading-relaxed break-all">
                  {errorMsg}
                </div>
              </div>
              <div className="flex items-center justify-end gap-2 pt-1">
                <button
                  type="button"
                  onClick={onClose}
                  className="px-3 py-1 rounded font-mono text-[10px] border border-[var(--border)] text-[var(--muted-foreground)] hover:bg-[var(--background)] transition-colors"
                >
                  CLOSE
                </button>
                <button
                  type="button"
                  onClick={onConfirmRotate}
                  data-testid="jira-webhook-rotate-retry"
                  className="flex items-center gap-1.5 px-3 py-1 rounded font-mono text-[10px] bg-[var(--neural-blue)]/10 text-[var(--neural-blue)] hover:bg-[var(--neural-blue)]/20 transition-colors"
                >
                  <RefreshCw size={10} />
                  RETRY
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>,
    document.body,
  )
}

export function IntegrationSettings({ open, onClose }: IntegrationSettingsProps) {
  // Y8 row 6 — surface the active tenant + project in the modal header so
  // operators can see which scope a save will land against. Optional
  // hooks: standalone test renders (no <Providers>) get null and the
  // scope strip is omitted.
  const tenantCtx = useTenantOptional()
  const projectCtx = useProjectOptional()
  const tenantName = tenantCtx
    ? (tenantCtx.tenants.find(t => t.id === tenantCtx.currentTenantId)?.name ?? null)
    : null
  const projectName = projectCtx
    ? (projectCtx.projects.find(p => p.project_id === projectCtx.currentProjectId)?.name ?? null)
    : null

  const [settingsData, setSettingsData] = useState<Record<string, Record<string, unknown>>>({})
  const [dirty, setDirty] = useState<Record<string, string | number | boolean>>({})
  const [saving, setSaving] = useState(false)
  const [saveFeedback, setSaveFeedback] = useState<SaveFeedback>(null)
  const [providers, setProviders] = useState<api.ProviderConfig[]>([])
  const [ollamaFailures, setOllamaFailures] = useState<api.OllamaToolFailuresResponse | null>(null)
  const [gerritWizardOpen, setGerritWizardOpen] = useState(false)
  // Y-prep.2 #288 — JIRA webhook secret rotate one-time-reveal dialog +
  // a local cache of the post-rotate masked preview. The settings GET
  // only returns `jira_secret: "configured"` / `""` (no real mask),
  // so after a rotate we remember the returned `secret_masked` here to
  // surface it inline next to the ROTATE button until the modal is
  // re-opened from a cold state.
  const [jiraRotateDialogOpen, setJiraRotateDialogOpen] = useState(false)
  const [jiraLastRotatedMasked, setJiraLastRotatedMasked] = useState<string>("")

  // Pulled out of the open-only effect so the SSE listener below can
  // reuse it without re-declaring the fetch pair inline.
  const refetchAll = useCallback(() => {
    api.getSettings().then(setSettingsData).catch(() => {})
    api.getProviders().then(r => setProviders(r.providers)).catch(() => {})
    // Z.6.5: fetch ollama tool-call failure counters for dashboard warning.
    api.getOllamaToolFailures().then(setOllamaFailures).catch(() => {})
  }, [])

  useEffect(() => {
    if (open) {
      setDirty({})  // Clear unsaved changes on fresh open
      setSaveFeedback(null)
      refetchAll()
    }
  }, [open, refetchAll])

  // Q.3-SUB-5 (#297) — cross-device non-LLM integration-settings SSE push.
  //
  // Before this subscription the modal only pulled fresh data on its
  // open transition (above). If the operator already had the modal
  // open on device A and device B's save landed on a different worker,
  // the SharedKV mirror kept values coherent across workers but the
  // UI on device A still showed the pre-save snapshot until a manual
  // close/re-open. This listener bridges the gap: any authoritative
  // change to the non-LLM subset published by the backend triggers a
  // fresh ``getSettings()`` + ``getProviders()`` pair so the modal
  // re-renders with the merged value. The listener is only active
  // while the modal is open — subscribeEvents shares one EventSource
  // across the app so this is effectively zero-cost when closed.
  //
  // Dirty-field preservation: ``setSettingsData`` replaces the saved
  // snapshot, but ``dirty`` stays untouched so an operator typing
  // into a field while an unrelated tab got overwritten from device
  // B doesn't lose their in-flight edit.
  useEffect(() => {
    if (!open) return
    const handle = api.subscribeEvents((ev) => {
      if (ev.event !== "integration.settings.updated") return
      refetchAll()
    })
    return () => { handle.close() }
  }, [open, refetchAll])

  // Auto-dismiss the save banner after 4s — long enough for the
  // operator to read the applied / rejected roll-up but short enough
  // that it doesn't linger past the next click.
  useEffect(() => {
    if (!saveFeedback) return
    const t = setTimeout(() => setSaveFeedback(null), 4000)
    return () => clearTimeout(t)
  }, [saveFeedback])

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
  // B14 Part D row 235 — per-section status for the CI/CD tab. Each
  // integration is independent (a site may run Jenkins without enabling
  // GitHub Actions), so the three sections each expose their own dot; the
  // tab-level `tabStatus.cicd` above stays an OR over them so the TabsList
  // badge lights up if ANY CI integration is configured.
  const cicdSectionStatus = {
    githubActions: getVal("ci", "github_actions_enabled") === "true",
    jenkins: (
      getVal("ci", "jenkins_enabled") === "true" &&
      hasValue("ci", "jenkins_url") &&
      hasValue("ci", "jenkins_api_token", "ci_jenkins_api_token")
    ),
    gitlabCi: getVal("ci", "gitlab_ci_enabled", "ci_gitlab_enabled") === "true",
  }
  const badgeClass = (ok: boolean) =>
    ok
      ? "bg-[var(--validation-emerald)]"
      : "bg-[var(--hardware-orange)]/60"
  const badgeTitle = (ok: boolean) =>
    ok ? "connected / configured" : "not configured"

  // B14 Part D row 236 — lifted from SettingsSection so the tab-level
  // badge can reflect error state. Each SettingsSection still owns its
  // own inline test result (the per-section green/red pill under the
  // section body); what we collect here is just the LAST status string
  // per integration, keyed by the `integration` prop name. That keeps
  // the section component's local state untouched so the existing
  // "TEST → expand → see result" interaction stays intact.
  const [probeResults, setProbeResults] = useState<Record<string, TestResult>>({})
  const recordProbeResult = useCallback((integration: string, result: TestResult) => {
    setProbeResults(prev => ({ ...prev, [integration]: result }))
  }, [])

  const resolveTabStatus = (tab: keyof typeof tabStatus): TabConnectionStatus => {
    // Error state is checked FIRST and wins over "configured": a stale
    // green dot on a tab with a failing probe would mislead the operator.
    // We treat any probe status !== "ok" as an error, which collapses
    // three backend vocabularies ("error", "not_configured" return from
    // an unconfigured integration, free-form "timeout", etc.) into the
    // single red banner — except for "not_configured" specifically, which
    // is expected when the user hasn't set anything up and should NOT
    // surface as an error banner. "not_configured" from a probe is
    // equivalent to tabStatus[tab] being false.
    const hits = (TAB_INTEGRATIONS[tab] ?? [])
      .map(i => probeResults[i])
      .filter((r): r is TestResult => r !== undefined)
    const firstError = hits.find(r => r.status !== "ok" && r.status !== "not_configured")
    if (firstError) return "error"
    return tabStatus[tab] ? "connected" : "not_configured"
  }

  const tabBadgeMessage = (tab: keyof typeof tabStatus): string | undefined => {
    const err = (TAB_INTEGRATIONS[tab] ?? [])
      .map(i => probeResults[i])
      .find(r => r && r.status !== "ok" && r.status !== "not_configured")
    return err?.message
  }

  const setVal = (configKey: string, value: string | boolean) => {
    setDirty(prev => ({ ...prev, [configKey]: value }))
  }

  const handleSave = async () => {
    if (Object.keys(dirty).length === 0) return
    setSaving(true)
    setSaveFeedback(null)
    try {
      const result = await api.updateSettings(dirty)
      setDirty({})
      // Refresh BOTH settings (for the masked key display) AND
      // providers (so the green "configured" dot updates without
      // the operator having to close+reopen the modal). Run in
      // parallel — no ordering dependency.
      const [fresh, refreshedProviders] = await Promise.all([
        api.getSettings().catch(() => settingsData),
        api.getProviders().catch(() => ({ providers })),
      ])
      setSettingsData(fresh)
      setProviders(refreshedProviders.providers)
      setSaveFeedback({
        kind: "ok",
        applied: result.applied ?? [],
        rejected: result.rejected ?? {},
      })
    } catch (e) {
      console.error("Save failed:", e)
      setSaveFeedback({
        kind: "error",
        message: e instanceof Error ? e.message : String(e),
      })
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
        <div className="flex items-start justify-between px-4 py-3 border-b border-[var(--border)] bg-[var(--secondary)]">
          <div className="flex flex-col gap-0.5 min-w-0">
            <div className="flex items-center gap-2">
              <Settings size={14} className="text-[var(--neural-blue)] shrink-0" />
              <h2 className="font-sans text-sm font-semibold tracking-fui text-[var(--neural-blue)]">SYSTEM INTEGRATIONS</h2>
            </div>
            {/* Y8 row 6 — scope strip. Tenant is always shown when the
                provider is mounted (current tenant is non-null after
                login). Project is shown when an active project is
                selected; otherwise we surface a "no project" hint so
                operators know project-scoped settings (tenant secrets
                shown elsewhere are tenant-only and unaffected) will
                land against the tenant default rather than a project.
                Hidden entirely outside the provider tree (test harness,
                pre-login). */}
            {tenantCtx && tenantCtx.currentTenantId && (
              <div
                data-testid="integration-settings-scope-label"
                className="font-mono text-[9px] text-[var(--muted-foreground)] flex flex-wrap items-center gap-x-2 gap-y-0.5 pl-[22px]"
              >
                <span data-testid="integration-settings-scope-tenant">
                  tenant:{" "}
                  <span className="text-[var(--neural-blue)]">
                    {tenantName ? `${tenantName} (${tenantCtx.currentTenantId})` : tenantCtx.currentTenantId}
                  </span>
                </span>
                <span aria-hidden="true">·</span>
                {projectCtx && projectCtx.currentProjectId ? (
                  <span data-testid="integration-settings-scope-project">
                    project:{" "}
                    <span className="text-[var(--neural-blue)]">
                      {projectName ? `${projectName} (${projectCtx.currentProjectId})` : projectCtx.currentProjectId}
                    </span>
                  </span>
                ) : (
                  <span
                    data-testid="integration-settings-scope-project-empty"
                    className="italic opacity-70"
                  >
                    no project selected
                  </span>
                )}
              </div>
            )}
          </div>
          <button onClick={onClose} className="p-1 rounded hover:bg-[var(--background)] transition-colors shrink-0">
            <X size={14} className="text-[var(--muted-foreground)]" />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto overflow-x-hidden p-3 space-y-2">

          {/* Save feedback banner — ok / error / partially-accepted
              states. Visible above the first settings section so the
              operator doesn't miss it on narrow modals that require
              scrolling. */}
          {saveFeedback && saveFeedback.kind === "ok" && (
            <div
              className="rounded px-3 py-2 border text-[10px] font-mono"
              style={{
                borderColor: Object.keys(saveFeedback.rejected).length > 0
                  ? "var(--hardware-orange)"
                  : "var(--validation-emerald)",
                backgroundColor: Object.keys(saveFeedback.rejected).length > 0
                  ? "color-mix(in srgb, var(--hardware-orange) 12%, transparent)"
                  : "color-mix(in srgb, var(--validation-emerald) 12%, transparent)",
                color: Object.keys(saveFeedback.rejected).length > 0
                  ? "var(--hardware-orange)"
                  : "var(--validation-emerald)",
              }}
            >
              <div className="font-semibold mb-0.5">
                {Object.keys(saveFeedback.rejected).length === 0
                  ? `✓ Saved ${saveFeedback.applied.length} field${saveFeedback.applied.length === 1 ? "" : "s"}`
                  : `⚠ Partially saved — ${saveFeedback.applied.length} applied, ${Object.keys(saveFeedback.rejected).length} rejected`}
              </div>
              {saveFeedback.applied.length > 0 && (
                <div className="opacity-80">applied: {saveFeedback.applied.join(", ")}</div>
              )}
              {Object.keys(saveFeedback.rejected).length > 0 && (
                <div className="opacity-80">
                  rejected: {Object.entries(saveFeedback.rejected).map(([k, v]) => `${k} (${v})`).join("; ")}
                </div>
              )}
            </div>
          )}
          {saveFeedback && saveFeedback.kind === "error" && (
            <div
              className="rounded px-3 py-2 border text-[10px] font-mono"
              style={{
                borderColor: "var(--critical-red)",
                backgroundColor: "color-mix(in srgb, var(--critical-red) 12%, transparent)",
                color: "var(--critical-red)",
              }}
            >
              <div className="font-semibold">✗ Save failed</div>
              <div className="opacity-80 mt-0.5">{saveFeedback.message}</div>
            </div>
          )}

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
                  {/* Z.6.4 — Ollama tool-calling compat badge. Only rendered when
                      the active provider is ollama AND the selected model has a
                      known compat entry from config/ollama_tool_calling.yaml. */}
                  {currentProvider === "ollama" &&
                    selectedProvider?.tool_calling_compat &&
                    currentModel &&
                    selectedProvider.tool_calling_compat[currentModel] && (
                      <div className="flex items-center gap-2 pl-[88px]">
                        <OllamaToolCallingBadge
                          compat={selectedProvider.tool_calling_compat[currentModel]}
                        />
                      </div>
                    )}
                  {/* Z.6.5 — Ollama tool-call fallback warning. Shown when the
                      adapter has degraded to pure-chat at least once (total > 0). */}
                  {currentProvider === "ollama" && ollamaFailures?.has_warning && (
                    <div className="pl-[88px]">
                      <OllamaToolFailureAlert failures={ollamaFailures} />
                    </div>
                  )}
                </>
              )
            })()}
            {/* Phase 5b-4 (#llm-credentials) — multi-account, DB-backed key
                manager replaces the legacy 8-password-field grid + single
                Ollama base URL input. Credentials are Fernet-encrypted at
                rest and persist across backend restarts. The provider /
                model / fallback-chain dropdowns above still go through
                PUT /runtime/settings (they are routing knobs, not
                credentials). */}
            <div className="pt-1.5">
              <LLMCredentialManagerSection />
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
              <TabStatusBadge
                status={resolveTabStatus("git")}
                testId="tab-status-badge-git"
                message={tabBadgeMessage("git")}
              />
              <SettingsSection title="GIT REPOSITORIES" integration="ssh" onTestResult={recordProbeResult}>
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
                {/* SSH key falls back into the GIT REPOSITORIES section because
                    its TEST button (`integration="ssh"`) probes the on-disk
                    private key — the natural home alongside the credential
                    registry. GitHub + GitLab settings live in their own
                    sections below so each forge owns its TEST button. */}
                <div className="pt-1.5 pb-0.5">
                  <span className="font-mono text-[8px] text-[var(--muted-foreground)] uppercase tracking-wider">Default Credentials (fallback)</span>
                </div>
                <SettingField label="SSH Key" value={getVal("git", "ssh_key_path")} onChange={v => setVal("git_ssh_key_path", v)} />
              </SettingsSection>

              {/* B14 Part E row 240: dedicated GITHUB section so the TEST
                  button hits the GitHub `_test_github` probe (`GET /user`)
                  and surfaces the resolved login + OAuth scopes alongside
                  the saved token. Pre-row-240 the GitHub field shared the
                  GIT REPOSITORIES section, whose TEST only exercised the
                  SSH key — so a working GitHub token + a missing SSH key
                  would render a misleading red dot, and a broken token
                  could hide behind a green dot. Splitting fixes both. */}
              <SettingsSection
                title="GITHUB"
                integration="github"
                onTestResult={recordProbeResult}
                onTest={async () => {
                  // 2026-04-22: two-path TEST logic.
                  // (a) If the operator has typed a NEW token
                  //     (``"github_token" in dirty``), probe THAT
                  //     candidate via ``testGitForgeToken`` which
                  //     accepts a token in the request body — no
                  //     save required, no dependency on
                  //     ``settings.github_token`` being populated
                  //     on whichever worker answers the request.
                  // (b) Otherwise (modal re-opened on a saved
                  //     deploy, operator hits TEST to re-validate
                  //     the stored token), fall through to
                  //     ``testIntegration("github")`` — that reads
                  //     the real ``settings.github_token`` on the
                  //     backend. Can't use the masked string from
                  //     ``settingsData`` as the candidate because
                  //     the mask contains literal asterisks that
                  //     GitHub would reject.
                  if ("github_token" in dirty) {
                    const token = String(dirty["github_token"]).trim()
                    if (!token) {
                      return { status: "not_configured", message: "GitHub token field is empty — type a token before hitting TEST." }
                    }
                    return await api.testGitForgeToken({ provider: "github", token })
                  }
                  return await api.testIntegration("github")
                }}
              >
                <SettingField
                  label="Token"
                  value={getVal("git", "github_token", "github_token")}
                  type="password"
                  onChange={v => setVal("github_token", v)}
                />
                <div className="font-mono text-[8px] text-[var(--muted-foreground)]/70 pt-1 leading-tight">
                  TEST calls <code>GET https://api.github.com/user</code> with this token and reports the resolved login + OAuth scopes.
                </div>
              </SettingsSection>

              {/* GitLab needs URL + Token — TEST hits `_test_gitlab` which
                  calls `GET /api/v4/version` against the configured base URL
                  and surfaces the GitLab instance version (the canonical
                  reachability + auth probe for self-managed deployments). */}
              <SettingsSection
                title="GITLAB"
                integration="gitlab"
                onTestResult={recordProbeResult}
                onTest={async () => {
                  // Same two-path logic as the GitHub section —
                  // probe the dirty candidate if present, else
                  // hit the legacy settings-reading endpoint.
                  // GitLab's probe also needs the URL (for self-
                  // hosted instances).
                  if ("gitlab_token" in dirty || "gitlab_url" in dirty) {
                    const token = ("gitlab_token" in dirty
                      ? String(dirty["gitlab_token"])
                      : String(settingsData["git"]?.["gitlab_token"] ?? "")
                    ).trim()
                    // URL legitimately may be blank (defaults to
                    // gitlab.com server-side) so don't pre-gate
                    // on that — only require a token.
                    const url = ("gitlab_url" in dirty
                      ? String(dirty["gitlab_url"])
                      : String(settingsData["git"]?.["gitlab_url"] ?? "")
                    ).trim()
                    if (!token) {
                      return { status: "not_configured", message: "GitLab token field is empty — type a token before hitting TEST." }
                    }
                    return await api.testGitForgeToken({ provider: "gitlab", token, url })
                  }
                  return await api.testIntegration("gitlab")
                }}
              >
                <SettingField
                  label="URL"
                  value={getVal("git", "gitlab_url", "gitlab_url")}
                  onChange={v => setVal("gitlab_url", v)}
                />
                <SettingField
                  label="Token"
                  value={getVal("git", "gitlab_token", "gitlab_token")}
                  type="password"
                  onChange={v => setVal("gitlab_token", v)}
                />
                <div className="font-mono text-[8px] text-[var(--muted-foreground)]/70 pt-1 leading-tight">
                  TEST calls <code>GET {`{URL}`}/api/v4/version</code> and reports the GitLab instance version. URL defaults to <code>https://gitlab.com</code> when blank.
                </div>
              </SettingsSection>

              {/* Phase 5-9 (#multi-account-forge): replaces the legacy
                  MultipleInstancesSection. AccountManagerSection talks to
                  the `/git-accounts` REST surface (per-tenant, encrypted at
                  rest, audit-logged), supports all four platforms, and
                  surfaces the legacy `*_token_map` blobs as a deprecation
                  banner until lifespan startup auto-migrates them into
                  `git_accounts` rows. */}
              <div className="border border-[var(--border)] rounded-md px-3 py-2 -mt-1">
                <AccountManagerSection
                  legacyGithubMap={String(settingsData["git"]?.["github_token_map"] ?? "")}
                  legacyGitlabMap={String(settingsData["git"]?.["gitlab_token_map"] ?? "")}
                />
              </div>
            </TabsContent>

            {/* Tab 2 — Gerrit Code Review (settings + wizard entry).
                `forceMount` + `data-[state=inactive]:hidden` keeps the Setup
                Wizard button in the DOM even when the Gerrit tab is not
                active so the GerritSetupWizardDialog can still be opened by
                code paths (e.g. deep-links, test harnesses) that assume a
                flat layout. Radix flips the `hidden` attribute on inactive
                panels so assistive tech still sees only the active tab. */}
            <TabsContent value="gerrit" forceMount className="space-y-2 mt-0 data-[state=inactive]:hidden">
              <TabStatusBadge
                status={resolveTabStatus("gerrit")}
                testId="tab-status-badge-gerrit"
                message={tabBadgeMessage("gerrit")}
              />
              <SettingsSection
                title="GERRIT CODE REVIEW"
                integration="gerrit"
                onTestResult={recordProbeResult}
                onTest={async () => {
                  // 2026-04-22: same two-path TEST logic as GitHub /
                  // GitLab above. If the operator has typed new
                  // Gerrit fields (ssh_host / ssh_port / url) and
                  // hasn't yet hit SAVE & APPLY, probe the candidate
                  // via ``testGitForgeToken({provider:"gerrit", …})``
                  // which accepts the values in the body and doesn't
                  // depend on which worker answers. Otherwise fall
                  // through to the saved-settings probe. Without
                  // this, the TEST button would always return
                  // "Gerrit is disabled" / "SSH host not set" when
                  // the user types into the fields but clicks TEST
                  // before saving — same UX footgun we fixed for the
                  // git forges in commit 0f4f4215.
                  const dirtyKeys = ["gerrit_ssh_host", "gerrit_ssh_port", "gerrit_url"] as const
                  const hasDirty = dirtyKeys.some(k => k in dirty)
                  if (hasDirty) {
                    const sshHost = ("gerrit_ssh_host" in dirty
                      ? String(dirty["gerrit_ssh_host"])
                      : String(settingsData["gerrit"]?.["ssh_host"] ?? "")
                    ).trim()
                    const sshPortRaw = ("gerrit_ssh_port" in dirty
                      ? String(dirty["gerrit_ssh_port"])
                      : String(settingsData["gerrit"]?.["ssh_port"] ?? "")
                    ).trim()
                    const url = ("gerrit_url" in dirty
                      ? String(dirty["gerrit_url"])
                      : String(settingsData["gerrit"]?.["url"] ?? "")
                    ).trim()
                    if (!sshHost) {
                      return { status: "not_configured", message: "Gerrit SSH host is empty — fill it before hitting TEST." }
                    }
                    const sshPort = parseInt(sshPortRaw, 10)
                    if (!Number.isFinite(sshPort) || sshPort < 1 || sshPort > 65535) {
                      return { status: "error", message: `Gerrit SSH port "${sshPortRaw}" is not in 1..65535.` }
                    }
                    return await api.testGitForgeToken({ provider: "gerrit", ssh_host: sshHost, ssh_port: sshPort, url })
                  }
                  return await api.testIntegration("gerrit")
                }}
              >
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
              <TabStatusBadge
                status={resolveTabStatus("webhooks")}
                testId="tab-status-badge-webhooks"
                message={tabBadgeMessage("webhooks")}
              />
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

              <SettingsSection title="JIRA ISSUE TRACKING" integration="jira" onTestResult={recordProbeResult}>
                <SettingField label="URL" value={getVal("jira", "url")} onChange={v => setVal("notification_jira_url", v)} />
                <SettingField label="Token" value={getVal("jira", "token")} type="password" onChange={v => setVal("notification_jira_token", v)} />
                <SettingField label="Project Key" value={getVal("jira", "project")} onChange={v => setVal("notification_jira_project", v)} />
                {/* Y-prep.3 (#289) — JIRA inbound automation routing knobs.
                    Both are routing config (not credentials) so we render
                    plaintext + show the built-in default in a footnote so
                    operators know what's running when the input is blank.
                    Settings keys are the bare field names (``jira_intake_label``
                    / ``jira_done_statuses``) — they live on Settings directly,
                    not under the ``notification_jira_*`` family, because the
                    inbound dispatcher reads them by their canonical name. */}
                <SettingField
                  label="Intake Label"
                  value={getVal("jira", "intake_label", "jira_intake_label")}
                  onChange={v => setVal("jira_intake_label", v)}
                />
                <SettingField
                  label="Done Statuses"
                  value={getVal("jira", "done_statuses", "jira_done_statuses")}
                  onChange={v => setVal("jira_done_statuses", v)}
                />
                <div className="font-mono text-[8px] text-[var(--muted-foreground)]/70 pt-1 leading-tight">
                  Intake label fires <code>intent_bridge.on_intake_queued</code> on <code>jira:issue_created</code> (default <code>omnisight-intake</code>). Done statuses are CSV; transitions into any listed value trigger artifact packaging (default <code>Done,Closed</code>).
                </div>
                {/* Y-prep.2 #288 — inbound webhook HMAC secret rotate.
                    Mirrors the Gerrit Wizard Step 5 one-time-reveal UX: the
                    plain value is shown exactly once in the confirm→reveal
                    modal; after close only a masked preview survives. The
                    plain `jira_webhook_secret` field above in INBOUND
                    WEBHOOK SECRETS still accepts manual entry for operators
                    who pre-generate the value out-of-band, but the rotate
                    button is the ergonomic default. */}
                <div className="flex items-center gap-2 pt-1" data-testid="jira-webhook-secret-rotate-row">
                  <label className="font-mono text-[9px] text-[var(--muted-foreground)] w-20 shrink-0">
                    Webhook Secret
                  </label>
                  <div className="flex-1 flex items-center gap-2">
                    <span
                      className="flex-1 font-mono text-[10px] px-2 py-1 rounded bg-[var(--background)]/50 border border-dashed border-[var(--border)] text-[var(--muted-foreground)] select-none"
                      data-testid="jira-webhook-secret-status"
                    >
                      {jiraLastRotatedMasked
                        ? `••• rotated • ${jiraLastRotatedMasked}`
                        : hasValue("webhooks", "jira_secret", "jira_webhook_secret")
                          ? "••• configured •••"
                          : "not configured"}
                    </span>
                    <button
                      type="button"
                      onClick={() => setJiraRotateDialogOpen(true)}
                      className="flex items-center gap-1 px-2 py-1 rounded font-mono text-[9px] bg-[var(--neural-blue)]/10 text-[var(--neural-blue)] hover:bg-[var(--neural-blue)]/20 transition-colors shrink-0"
                      title="Mint a fresh JIRA webhook secret (one-time reveal)"
                      data-testid="jira-webhook-secret-rotate-button"
                    >
                      <RefreshCw size={10} />
                      ROTATE WEBHOOK SECRET
                    </button>
                  </div>
                </div>
              </SettingsSection>

              <SettingsSection title="SLACK NOTIFICATIONS" integration="slack" onTestResult={recordProbeResult}>
                <SettingField label="Webhook" value={getVal("slack", "webhook")} type="password" onChange={v => setVal("notification_slack_webhook", v)} />
                <SettingField label="Mention ID" value={getVal("slack", "mention")} onChange={v => setVal("notification_slack_mention", v)} />
              </SettingsSection>
            </TabsContent>

            {/* Tab 4 — Outbound CI/CD triggers. Config fields already exist in
                backend/config.py (ci_*) and are whitelisted in
                backend/routers/integration.py `_UPDATABLE_FIELDS`.

                B14 Part D row 235: each of the three sections (GitHub
                Actions / Jenkins / GitLab CI) carries a per-section status
                dot so an operator can tell at a glance which pipelines are
                wired up. GitHub Actions + GitLab CI are single-toggle
                (they reuse the Git tab's token), so "configured" === toggle
                is ON. Jenkins needs toggle + URL + API token all set
                (username is optional — many Jenkins deployments use
                token-only auth) because the backend trigger in
                `_trigger_ci_pipelines` silently no-ops if URL or token is
                missing; a green dot on "enabled but URL empty" would lie
                to the operator. */}
            <TabsContent value="cicd" className="space-y-2 mt-0">
              <TabStatusBadge
                status={resolveTabStatus("cicd")}
                testId="tab-status-badge-cicd"
                message={tabBadgeMessage("cicd")}
              />
              <SettingsSection
                title="GITHUB ACTIONS"
                status={cicdSectionStatus.githubActions}
                statusTestId="cicd-section-dot-github-actions"
              >
                <ToggleField
                  label="Enabled"
                  value={getVal("ci", "github_actions_enabled") === "true"}
                  onChange={v => setVal("ci_github_actions_enabled", v)}
                />
                <div className="font-mono text-[8px] text-[var(--muted-foreground)]/70 pt-1 leading-tight">
                  Uses the GitHub Token from the Git tab.
                </div>
              </SettingsSection>

              <SettingsSection
                title="JENKINS"
                status={cicdSectionStatus.jenkins}
                statusTestId="cicd-section-dot-jenkins"
              >
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

              <SettingsSection
                title="GITLAB CI"
                status={cicdSectionStatus.gitlabCi}
                statusTestId="cicd-section-dot-gitlab-ci"
              >
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
      <JiraWebhookSecretRotateDialog
        open={jiraRotateDialogOpen}
        onClose={() => setJiraRotateDialogOpen(false)}
        onRotated={(masked) => setJiraLastRotatedMasked(masked)}
      />
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
