"use client"

/**
 * BP.A2A.6 -- operator external A2A agent registry page.
 *
 * Auth gating
 * -----------
 * All authenticated roles may inspect registered endpoints. Operator
 * and higher roles may register/update endpoint bindings and toggle the
 * kill-switch. The backend remains authoritative via require_operator.
 *
 * Module-global state audit
 * -------------------------
 * None introduced. The page keeps per-component React state only and
 * calls typed `lib/api.ts` wrappers. Cross-worker registry durability is
 * a backend store concern and is not cached in the browser.
 */

import { useCallback, useEffect, useMemo, useState } from "react"
import Link from "next/link"
import {
  ArrowLeft,
  Bot,
  Check,
  ChevronRight,
  CircleAlert,
  Loader2,
  Plus,
  RefreshCw,
  ShieldAlert,
  ToggleLeft,
  ToggleRight,
  X,
} from "lucide-react"
import {
  ApiError,
  listExternalAgents,
  patchExternalAgent,
  registerExternalAgent,
  type ExternalAgentAuthMode,
  type ExternalAgentRow,
} from "@/lib/api"
import { useAuth } from "@/lib/auth-context"

const ROLE_ORDER = ["viewer", "operator", "admin", "super_admin"]
const AUTH_MODES: ExternalAgentAuthMode[] = ["none", "bearer", "oauth2"]
const AGENT_ID_PATTERN = /^[a-z0-9][a-z0-9_-]{1,63}$/

function roleAtLeast(role: string | undefined, minRole: string): boolean {
  const have = role ? ROLE_ORDER.indexOf(role) : -1
  const need = ROLE_ORDER.indexOf(minRole)
  return have >= 0 && need >= 0 && have >= need
}

function splitList(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean)
}

function formatTime(value: string | null): string {
  if (!value) return "-"
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  return d.toISOString().slice(0, 19).replace("T", " ")
}

interface CreateState {
  agentId: string
  displayName: string
  baseUrl: string
  agentName: string
  description: string
  authMode: ExternalAgentAuthMode
  tokenRef: string
  tags: string
  capabilities: string
  submitting: boolean
  error: string | null
}

const EMPTY_CREATE: CreateState = {
  agentId: "",
  displayName: "",
  baseUrl: "",
  agentName: "orchestrator",
  description: "",
  authMode: "none",
  tokenRef: "",
  tags: "",
  capabilities: "",
  submitting: false,
  error: null,
}

export default function AdminExternalAgentsPage() {
  const { user, authMode, loading: authLoading } = useAuth()
  const [rows, setRows] = useState<ExternalAgentRow[]>([])
  const [canRegisterFromServer, setCanRegisterFromServer] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busyAgentId, setBusyAgentId] = useState<string | null>(null)
  const [rowError, setRowError] = useState<{ agentId: string; message: string } | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [createState, setCreateState] = useState<CreateState>(EMPTY_CREATE)

  const canRegister = useMemo(() => {
    if (authMode === "open") return true
    return canRegisterFromServer && roleAtLeast(user?.role, "operator")
  }, [authMode, canRegisterFromServer, user?.role])

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await listExternalAgents()
      setRows(res.external_agents)
      setCanRegisterFromServer(res.can_register)
    } catch (exc) {
      const msg = exc instanceof Error ? exc.message : String(exc)
      setError(msg)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (authLoading) return
    const timer = window.setTimeout(() => {
      void refresh()
    }, 0)
    return () => window.clearTimeout(timer)
  }, [authLoading, refresh])

  const onCreate = useCallback(async () => {
    setCreateState((s) => ({ ...s, submitting: true, error: null }))
    if (!AGENT_ID_PATTERN.test(createState.agentId)) {
      setCreateState((s) => ({
        ...s,
        submitting: false,
        error: "Agent id must be lowercase and may contain digits, dash, or underscore.",
      }))
      return
    }
    if (!createState.displayName.trim() || !createState.baseUrl.trim() || !createState.agentName.trim()) {
      setCreateState((s) => ({
        ...s,
        submitting: false,
        error: "Display name, base URL, and remote agent name are required.",
      }))
      return
    }
    if (createState.authMode !== "none" && !createState.tokenRef.trim()) {
      setCreateState((s) => ({
        ...s,
        submitting: false,
        error: "Token ref is required for bearer or OAuth2 endpoints.",
      }))
      return
    }
    try {
      await registerExternalAgent({
        agent_id: createState.agentId,
        display_name: createState.displayName.trim(),
        base_url: createState.baseUrl.trim(),
        agent_name: createState.agentName.trim(),
        description: createState.description.trim(),
        auth_mode: createState.authMode,
        token_ref: createState.authMode === "none" ? "" : createState.tokenRef.trim(),
        enabled: true,
        tags: splitList(createState.tags),
        capabilities: splitList(createState.capabilities),
      })
      setCreateOpen(false)
      setCreateState(EMPTY_CREATE)
      await refresh()
    } catch (exc) {
      const msg =
        exc instanceof ApiError
          ? `${exc.status}: ${(exc.parsed as { detail?: string } | null)?.detail ?? exc.body}`
          : exc instanceof Error
            ? exc.message
            : String(exc)
      setCreateState((s) => ({ ...s, submitting: false, error: msg }))
    }
  }, [createState, refresh])

  const onToggle = useCallback(
    async (row: ExternalAgentRow) => {
      setBusyAgentId(row.agent_id)
      setRowError(null)
      try {
        const res = await patchExternalAgent(row.agent_id, !row.enabled)
        setRows((current) =>
          current.map((r) =>
            r.agent_id === row.agent_id ? res.external_agent : r,
          ),
        )
      } catch (exc) {
        const detail =
          exc instanceof ApiError
            ? (exc.parsed as { detail?: string } | null)?.detail ?? exc.body
            : exc instanceof Error
              ? exc.message
              : String(exc)
        setRowError({ agentId: row.agent_id, message: detail || "toggle failed" })
      } finally {
        setBusyAgentId(null)
      }
    },
    [],
  )

  if (authLoading) {
    return (
      <main className="min-h-screen flex items-center justify-center bg-[var(--background)] text-[var(--foreground)]">
        <div className="font-mono text-xs text-[var(--muted-foreground)] flex items-center gap-2">
          <Loader2 size={14} className="animate-spin" />
          Verifying operator session...
        </div>
      </main>
    )
  }

  return (
    <main
      className="min-h-screen bg-[var(--background)] text-[var(--foreground)] p-6 md:p-10"
      data-testid="admin-external-agents-page"
    >
      <div className="max-w-6xl mx-auto">
        <header className="flex items-center justify-between mb-6">
          <div>
            <div className="flex items-center gap-2 text-[10px] font-mono text-[var(--muted-foreground)] mb-1">
              <Link href="/" className="hover:text-[var(--foreground)] inline-flex items-center gap-1">
                <ArrowLeft size={10} /> dashboard
              </Link>
              <ChevronRight size={10} />
              <span>admin</span>
              <ChevronRight size={10} />
              <span className="text-[var(--foreground)]">external agents</span>
            </div>
            <h1 className="text-xl font-semibold flex items-center gap-2">
              <Bot size={20} />
              External A2A Agents
            </h1>
            <p className="text-xs text-[var(--muted-foreground)] mt-1">
              Outbound Agent-to-Agent endpoint bindings for partner and third-party agents.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => void refresh()}
              disabled={loading}
              className="inline-flex items-center gap-1 px-3 py-1.5 rounded border border-[var(--border)] bg-[var(--card)] hover:bg-[var(--secondary)]/40 text-xs font-mono disabled:opacity-50"
              data-testid="external-agents-refresh"
            >
              <RefreshCw size={12} className={loading ? "animate-spin" : ""} />
              Refresh
            </button>
            <button
              type="button"
              onClick={() => setCreateOpen(true)}
              disabled={!canRegister}
              className="inline-flex items-center gap-1 px-3 py-1.5 rounded border border-[var(--border)] bg-[var(--card)] hover:bg-[var(--secondary)]/40 text-xs font-mono disabled:opacity-50"
              data-testid="external-agents-open-create"
            >
              <Plus size={12} />
              Register
            </button>
          </div>
        </header>

        {!canRegister && (
          <div
            className="mb-4 rounded border border-[var(--border)] bg-[var(--card)] p-3 text-xs font-mono text-[var(--muted-foreground)] flex items-center gap-2"
            data-testid="external-agents-readonly"
          >
            <ShieldAlert size={12} />
            <span>Read-only session. Role operator or higher is required to register endpoints.</span>
          </div>
        )}

        {error && (
          <div
            className="mb-4 rounded border border-[var(--destructive)]/40 bg-[var(--destructive)]/10 p-3 text-xs font-mono text-[var(--destructive)] flex items-center gap-2"
            data-testid="external-agents-error"
          >
            <CircleAlert size={12} />
            <span>Failed to load external agents: {error}</span>
          </div>
        )}

        <div className="rounded border border-[var(--border)] bg-[var(--card)] overflow-x-auto">
          <table className="w-full font-mono text-xs">
            <thead>
              <tr className="border-b border-[var(--border)] text-[10px] text-[var(--muted-foreground)]">
                <th className="text-left px-3 py-2">agent</th>
                <th className="text-left px-3 py-2">base URL</th>
                <th className="text-left px-3 py-2">remote</th>
                <th className="text-left px-3 py-2">auth</th>
                <th className="text-left px-3 py-2">health</th>
                <th className="text-left px-3 py-2">updated</th>
                <th className="text-right px-3 py-2">actions</th>
              </tr>
            </thead>
            <tbody>
              {loading && (
                <tr>
                  <td colSpan={7} className="text-center py-8 text-[var(--muted-foreground)]">
                    <Loader2 size={14} className="animate-spin inline-block mr-2" />
                    Loading external agents...
                  </td>
                </tr>
              )}
              {!loading && rows.length === 0 && (
                <tr>
                  <td colSpan={7} className="text-center py-8 text-[var(--muted-foreground)]">
                    No external A2A agents registered.
                  </td>
                </tr>
              )}
              {!loading && rows.map((row) => (
                <tr key={row.agent_id} className="border-b border-[var(--border)] last:border-0">
                  <td className="px-3 py-2 align-top">
                    <div className="font-semibold text-[var(--foreground)]">{row.display_name}</div>
                    <div className="text-[10px] text-[var(--muted-foreground)]">{row.agent_id}</div>
                    {rowError?.agentId === row.agent_id && (
                      <div className="text-[10px] text-[var(--destructive)] mt-1">{rowError.message}</div>
                    )}
                  </td>
                  <td className="px-3 py-2 align-top max-w-xs break-all">{row.base_url}</td>
                  <td className="px-3 py-2 align-top">{row.agent_name}</td>
                  <td className="px-3 py-2 align-top">{row.auth_mode}</td>
                  <td className="px-3 py-2 align-top">{row.health_status}</td>
                  <td className="px-3 py-2 align-top">{formatTime(row.updated_at)}</td>
                  <td className="px-3 py-2 align-top text-right">
                    <button
                      type="button"
                      onClick={() => void onToggle(row)}
                      disabled={!canRegister || busyAgentId === row.agent_id}
                      className="inline-flex items-center justify-center w-8 h-8 rounded border border-[var(--border)] hover:bg-[var(--secondary)]/40 disabled:opacity-50"
                      aria-label={row.enabled ? `Disable ${row.agent_id}` : `Enable ${row.agent_id}`}
                    >
                      {busyAgentId === row.agent_id ? (
                        <Loader2 size={14} className="animate-spin" />
                      ) : row.enabled ? (
                        <ToggleRight size={15} />
                      ) : (
                        <ToggleLeft size={15} />
                      )}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {createOpen && (
        <div className="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-4">
          <div className="w-full max-w-2xl rounded border border-[var(--border)] bg-[var(--card)] p-5 font-mono">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-sm font-semibold flex items-center gap-2">
                <Bot size={16} />
                Register External Agent
              </h2>
              <button
                type="button"
                onClick={() => setCreateOpen(false)}
                className="inline-flex items-center justify-center w-8 h-8 rounded border border-[var(--border)] hover:bg-[var(--secondary)]/40"
                aria-label="Close"
              >
                <X size={14} />
              </button>
            </div>

            {createState.error && (
              <div className="mb-4 rounded border border-[var(--destructive)]/40 bg-[var(--destructive)]/10 p-3 text-xs text-[var(--destructive)]">
                {createState.error}
              </div>
            )}

            <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-xs">
              <label className="space-y-1">
                <span className="text-[var(--muted-foreground)]">agent id</span>
                <input
                  value={createState.agentId}
                  onChange={(e) => setCreateState((s) => ({ ...s, agentId: e.target.value }))}
                  className="w-full rounded border border-[var(--border)] bg-[var(--background)] px-3 py-2"
                  placeholder="threat-intel-a2a"
                />
              </label>
              <label className="space-y-1">
                <span className="text-[var(--muted-foreground)]">display name</span>
                <input
                  value={createState.displayName}
                  onChange={(e) => setCreateState((s) => ({ ...s, displayName: e.target.value }))}
                  className="w-full rounded border border-[var(--border)] bg-[var(--background)] px-3 py-2"
                  placeholder="Threat Intel Agent"
                />
              </label>
              <label className="space-y-1 md:col-span-2">
                <span className="text-[var(--muted-foreground)]">base URL</span>
                <input
                  value={createState.baseUrl}
                  onChange={(e) => setCreateState((s) => ({ ...s, baseUrl: e.target.value }))}
                  className="w-full rounded border border-[var(--border)] bg-[var(--background)] px-3 py-2"
                  placeholder="https://agent.example.com"
                />
              </label>
              <label className="space-y-1">
                <span className="text-[var(--muted-foreground)]">remote agent name</span>
                <input
                  value={createState.agentName}
                  onChange={(e) => setCreateState((s) => ({ ...s, agentName: e.target.value }))}
                  className="w-full rounded border border-[var(--border)] bg-[var(--background)] px-3 py-2"
                  placeholder="orchestrator"
                />
              </label>
              <label className="space-y-1">
                <span className="text-[var(--muted-foreground)]">auth mode</span>
                <select
                  value={createState.authMode}
                  onChange={(e) =>
                    setCreateState((s) => ({ ...s, authMode: e.target.value as ExternalAgentAuthMode }))
                  }
                  className="w-full rounded border border-[var(--border)] bg-[var(--background)] px-3 py-2"
                >
                  {AUTH_MODES.map((mode) => (
                    <option key={mode} value={mode}>{mode}</option>
                  ))}
                </select>
              </label>
              <label className="space-y-1">
                <span className="text-[var(--muted-foreground)]">token ref</span>
                <input
                  value={createState.tokenRef}
                  onChange={(e) => setCreateState((s) => ({ ...s, tokenRef: e.target.value }))}
                  disabled={createState.authMode === "none"}
                  className="w-full rounded border border-[var(--border)] bg-[var(--background)] px-3 py-2 disabled:opacity-50"
                  placeholder="secret:a2a-threat-intel"
                />
              </label>
              <label className="space-y-1">
                <span className="text-[var(--muted-foreground)]">tags</span>
                <input
                  value={createState.tags}
                  onChange={(e) => setCreateState((s) => ({ ...s, tags: e.target.value }))}
                  className="w-full rounded border border-[var(--border)] bg-[var(--background)] px-3 py-2"
                  placeholder="secops, threat-intel"
                />
              </label>
              <label className="space-y-1 md:col-span-2">
                <span className="text-[var(--muted-foreground)]">capabilities</span>
                <input
                  value={createState.capabilities}
                  onChange={(e) => setCreateState((s) => ({ ...s, capabilities: e.target.value }))}
                  className="w-full rounded border border-[var(--border)] bg-[var(--background)] px-3 py-2"
                  placeholder="cve_triage, ioc_enrichment"
                />
              </label>
              <label className="space-y-1 md:col-span-2">
                <span className="text-[var(--muted-foreground)]">description</span>
                <textarea
                  value={createState.description}
                  onChange={(e) => setCreateState((s) => ({ ...s, description: e.target.value }))}
                  className="w-full min-h-20 rounded border border-[var(--border)] bg-[var(--background)] px-3 py-2"
                  placeholder="Outbound A2A endpoint for partner agent invocation."
                />
              </label>
            </div>

            <div className="flex items-center justify-end gap-2 mt-5">
              <button
                type="button"
                onClick={() => setCreateOpen(false)}
                className="inline-flex items-center gap-1 px-3 py-1.5 rounded border border-[var(--border)] hover:bg-[var(--secondary)]/40 text-xs"
              >
                <X size={12} />
                Cancel
              </button>
              <button
                type="button"
                onClick={() => void onCreate()}
                disabled={createState.submitting}
                className="inline-flex items-center gap-1 px-3 py-1.5 rounded border border-[var(--border)] bg-[var(--foreground)] text-[var(--background)] text-xs disabled:opacity-50"
              >
                {createState.submitting ? (
                  <Loader2 size={12} className="animate-spin" />
                ) : (
                  <Check size={12} />
                )}
                Save
              </button>
            </div>
          </div>
        </div>
      )}
    </main>
  )
}
