"use client"

import { useCallback, useEffect, useState } from "react"
import { Key, Plus, RotateCw, Trash2, ShieldOff, ShieldCheck, Copy, Eye, EyeOff } from "lucide-react"
import {
  listApiKeys,
  createApiKey,
  rotateApiKey,
  revokeApiKey,
  enableApiKey,
  deleteApiKey,
  type ApiKeyItem,
} from "@/lib/api"

function formatTime(epoch: number | null): string {
  if (!epoch) return "—"
  const d = new Date(epoch * 1000)
  return d.toLocaleString(undefined, {
    month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit",
  })
}

export function ApiKeyManagementPanel() {
  const [keys, setKeys] = useState<ApiKeyItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const [showCreate, setShowCreate] = useState(false)
  const [newName, setNewName] = useState("")
  const [newScopes, setNewScopes] = useState("*")
  const [newSecret, setNewSecret] = useState("")
  const [showSecret, setShowSecret] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      setLoading(true)
      const res = await listApiKeys()
      setKeys(res.items)
      setError("")
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to load API keys")
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { refresh() }, [refresh])

  const handleCreate = async () => {
    if (!newName.trim()) return
    try {
      const scopes = newScopes.split(",").map(s => s.trim()).filter(Boolean)
      const res = await createApiKey(newName.trim(), scopes)
      setNewSecret(res.secret)
      setShowSecret(true)
      setNewName("")
      setNewScopes("*")
      setShowCreate(false)
      await refresh()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to create key")
    }
  }

  const handleRotate = async (keyId: string) => {
    try {
      const res = await rotateApiKey(keyId)
      setNewSecret(res.secret)
      setShowSecret(true)
      await refresh()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to rotate key")
    }
  }

  const handleRevoke = async (keyId: string) => {
    try {
      await revokeApiKey(keyId)
      await refresh()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to revoke key")
    }
  }

  const handleEnable = async (keyId: string) => {
    try {
      await enableApiKey(keyId)
      await refresh()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to enable key")
    }
  }

  const handleDelete = async (keyId: string) => {
    try {
      await deleteApiKey(keyId)
      setConfirmDelete(null)
      await refresh()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to delete key")
    }
  }

  const copySecret = async () => {
    try {
      await navigator.clipboard.writeText(newSecret)
    } catch {
      /* clipboard may not be available */
    }
  }

  return (
    <div className="font-mono text-xs space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold flex items-center gap-2">
          <Key size={14} /> API Keys
        </h3>
        <button
          onClick={() => setShowCreate(v => !v)}
          className="flex items-center gap-1 px-2 py-1 rounded bg-[var(--neural-blue)]/10 text-[var(--neural-blue)] hover:bg-[var(--neural-blue)]/20 transition-colors"
        >
          <Plus size={12} /> New Key
        </button>
      </div>

      {error && (
        <div className="p-2 rounded bg-[var(--destructive)]/10 text-[var(--destructive)]">
          {error}
        </div>
      )}

      {newSecret && (
        <div className="p-3 rounded border border-[var(--warning,orange)]/40 bg-[var(--warning,orange)]/5 space-y-2">
          <div className="font-semibold text-[var(--warning,orange)]">
            Save this secret — it will not be shown again
          </div>
          <div className="flex items-center gap-2">
            <code className="flex-1 p-2 rounded bg-[var(--secondary)] break-all select-all">
              {showSecret ? newSecret : "••••••••••••••••••••"}
            </code>
            <button onClick={() => setShowSecret(v => !v)} className="p-1 hover:bg-[var(--secondary)] rounded" title={showSecret ? "Hide" : "Reveal"}>
              {showSecret ? <EyeOff size={14} /> : <Eye size={14} />}
            </button>
            <button onClick={copySecret} className="p-1 hover:bg-[var(--secondary)] rounded" title="Copy">
              <Copy size={14} />
            </button>
          </div>
          <button
            onClick={() => { setNewSecret(""); setShowSecret(false) }}
            className="text-[10px] underline text-[var(--muted-foreground)]"
          >
            Dismiss
          </button>
        </div>
      )}

      {showCreate && (
        <div className="p-3 rounded border border-[var(--border)] space-y-2">
          <input
            type="text"
            placeholder="Key name (e.g. CI pipeline)"
            value={newName}
            onChange={e => setNewName(e.target.value)}
            className="w-full px-2 py-1.5 rounded bg-[var(--secondary)] border border-[var(--border)] text-[var(--foreground)] placeholder:text-[var(--muted-foreground)]"
            autoFocus
          />
          <input
            type="text"
            placeholder="Scopes (comma-separated, * for all)"
            value={newScopes}
            onChange={e => setNewScopes(e.target.value)}
            className="w-full px-2 py-1.5 rounded bg-[var(--secondary)] border border-[var(--border)] text-[var(--foreground)] placeholder:text-[var(--muted-foreground)]"
          />
          <div className="flex gap-2">
            <button
              onClick={handleCreate}
              disabled={!newName.trim()}
              className="px-3 py-1.5 rounded bg-[var(--neural-blue)] text-white disabled:opacity-40"
            >
              Create
            </button>
            <button
              onClick={() => setShowCreate(false)}
              className="px-3 py-1.5 rounded bg-[var(--secondary)] text-[var(--foreground)]"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {loading ? (
        <div className="text-[var(--muted-foreground)] py-4 text-center">Loading...</div>
      ) : keys.length === 0 ? (
        <div className="text-[var(--muted-foreground)] py-4 text-center">
          No API keys yet. Create one to replace the legacy bearer token.
        </div>
      ) : (
        <div className="space-y-2">
          {keys.map(k => (
            <div
              key={k.id}
              className={`p-3 rounded border transition-colors ${
                k.enabled
                  ? "border-[var(--border)] bg-[var(--card)]"
                  : "border-[var(--destructive)]/30 bg-[var(--destructive)]/5 opacity-60"
              }`}
            >
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <Key size={12} className={k.enabled ? "text-[var(--neural-blue)]" : "text-[var(--muted-foreground)]"} />
                  <span className="font-semibold">{k.name}</span>
                  {k.name === "legacy-bearer" && (
                    <span className="px-1.5 py-0.5 rounded bg-[var(--warning,orange)]/15 text-[var(--warning,orange)] text-[9px] uppercase">
                      legacy
                    </span>
                  )}
                  {!k.enabled && (
                    <span className="px-1.5 py-0.5 rounded bg-[var(--destructive)]/15 text-[var(--destructive)] text-[9px] uppercase">
                      revoked
                    </span>
                  )}
                </div>
                <div className="flex items-center gap-1">
                  <button
                    onClick={() => handleRotate(k.id)}
                    className="p-1 rounded hover:bg-[var(--secondary)]"
                    title="Rotate secret"
                  >
                    <RotateCw size={12} />
                  </button>
                  {k.enabled ? (
                    <button
                      onClick={() => handleRevoke(k.id)}
                      className="p-1 rounded hover:bg-[var(--destructive)]/10 text-[var(--destructive)]"
                      title="Revoke"
                    >
                      <ShieldOff size={12} />
                    </button>
                  ) : (
                    <button
                      onClick={() => handleEnable(k.id)}
                      className="p-1 rounded hover:bg-[var(--neural-blue)]/10 text-[var(--neural-blue)]"
                      title="Re-enable"
                    >
                      <ShieldCheck size={12} />
                    </button>
                  )}
                  {confirmDelete === k.id ? (
                    <button
                      onClick={() => handleDelete(k.id)}
                      className="px-2 py-0.5 rounded bg-[var(--destructive)] text-white text-[10px]"
                    >
                      Confirm
                    </button>
                  ) : (
                    <button
                      onClick={() => setConfirmDelete(k.id)}
                      className="p-1 rounded hover:bg-[var(--destructive)]/10 text-[var(--destructive)]"
                      title="Delete permanently"
                    >
                      <Trash2 size={12} />
                    </button>
                  )}
                </div>
              </div>
              <div className="mt-1.5 grid grid-cols-2 gap-x-4 gap-y-0.5 text-[var(--muted-foreground)] text-[10px]">
                <div>Prefix: <span className="text-[var(--foreground)]">{k.key_prefix}...</span></div>
                <div>Created by: <span className="text-[var(--foreground)]">{k.created_by || "—"}</span></div>
                <div>Scopes: <span className="text-[var(--foreground)]">{k.scopes.join(", ")}</span></div>
                <div>Last used: <span className="text-[var(--foreground)]">{formatTime(k.last_used_at)}</span></div>
                <div>Last IP: <span className="text-[var(--foreground)]">{k.last_used_ip || "—"}</span></div>
                <div>ID: <span className="text-[var(--foreground)]">{k.id}</span></div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
