"use client"

import { useState, useEffect, useCallback } from "react"
import { createPortal } from "react-dom"
import {
  X, Check, AlertTriangle, Loader, Shield, Globe, Server,
  Link2, Trash2, RefreshCw, ChevronRight, ExternalLink, Eye, EyeOff,
} from "lucide-react"
import * as api from "@/lib/api"

interface CloudflareTunnelSetupProps {
  open: boolean
  onClose: () => void
}

type WizardStep = "token" | "select" | "hostnames" | "review" | "provisioning" | "done"

interface Account { id: string; name: string }
interface Zone { id: string; name: string }
interface ProvisionEvent { step: string; status: string; detail: string }
interface TunnelStatus {
  tunnel_id: string | null
  tunnel_name: string | null
  tunnel_status: string | null
  connector_online: boolean
  dns_records: Array<{ name: string; content: string }>
  hostnames: string[]
  provisioned: boolean
}

const REQUIRED_SCOPES = [
  "Account:Cloudflare Tunnel:Edit",
  "Zone:DNS:Edit",
  "Account:Account Settings:Read",
]

const TOKEN_HELP_URL = "https://developers.cloudflare.com/fundamentals/api/get-started/create-token/"

function StepIndicator({ current, steps }: { current: number; steps: string[] }) {
  return (
    <div className="flex items-center gap-1 mb-4">
      {steps.map((label, i) => (
        <div key={label} className="flex items-center gap-1">
          <div className={`w-5 h-5 rounded-full flex items-center justify-center text-[8px] font-mono font-bold ${
            i < current ? "bg-[var(--validation-emerald)] text-white"
            : i === current ? "bg-[var(--neural-blue)] text-white"
            : "bg-[var(--secondary)] text-[var(--muted-foreground)]"
          }`}>
            {i < current ? <Check size={10} /> : i + 1}
          </div>
          <span className={`text-[8px] font-mono ${
            i === current ? "text-[var(--neural-blue)] font-bold" : "text-[var(--muted-foreground)]"
          }`}>{label}</span>
          {i < steps.length - 1 && <ChevronRight size={8} className="text-[var(--muted-foreground)]" />}
        </div>
      ))}
    </div>
  )
}

function StatusBadge({ online }: { online: boolean }) {
  return (
    <span className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[8px] font-mono font-bold ${
      online ? "bg-[var(--validation-emerald)]/10 text-[var(--validation-emerald)]"
      : "bg-[var(--critical-red)]/10 text-[var(--critical-red)]"
    }`}>
      <span className={`w-1.5 h-1.5 rounded-full ${online ? "bg-[var(--validation-emerald)]" : "bg-[var(--critical-red)]"}`} />
      {online ? "ONLINE" : "OFFLINE"}
    </span>
  )
}

export default function CloudflareTunnelSetup({ open, onClose }: CloudflareTunnelSetupProps) {
  const [step, setStep] = useState<WizardStep>("token")
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Step 1: Token
  const [apiToken, setApiToken] = useState("")
  const [showToken, setShowToken] = useState(false)
  const [tokenFingerprint, setTokenFingerprint] = useState("")

  // Step 2: Account/Zone selection
  const [accounts, setAccounts] = useState<Account[]>([])
  const [selectedAccount, setSelectedAccount] = useState("")
  const [zones, setZones] = useState<Zone[]>([])
  const [selectedZone, setSelectedZone] = useState("")
  const [selectedZoneName, setSelectedZoneName] = useState("")

  // Step 3: Hostnames
  const [hostnames, setHostnames] = useState<string[]>([])
  const [tunnelName, setTunnelName] = useState("omnisight")

  // Step 4-5: Provisioning
  const [provisionEvents, setProvisionEvents] = useState<ProvisionEvent[]>([])

  // Existing tunnel state
  const [existingStatus, setExistingStatus] = useState<TunnelStatus | null>(null)
  const [showManage, setShowManage] = useState(false)

  const checkExisting = useCallback(async () => {
    try {
      const resp = await fetch("/api/v1/cloudflare/status")
      if (resp.ok) {
        const data = await resp.json()
        if (data.provisioned) {
          setExistingStatus(data)
          setShowManage(true)
        }
      }
    } catch { /* no existing tunnel */ }
  }, [])

  useEffect(() => {
    if (open) checkExisting()
  }, [open, checkExisting])

  const resetWizard = useCallback(() => {
    setStep("token")
    setLoading(false)
    setError(null)
    setApiToken("")
    setShowToken(false)
    setTokenFingerprint("")
    setAccounts([])
    setSelectedAccount("")
    setZones([])
    setSelectedZone("")
    setSelectedZoneName("")
    setHostnames([])
    setTunnelName("omnisight")
    setProvisionEvents([])
    setShowManage(false)
    setExistingStatus(null)
  }, [])

  // ── Step 1: Validate token ──
  const handleValidateToken = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const resp = await fetch("/api/v1/cloudflare/validate-token", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ api_token: apiToken }),
      })
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: "Validation failed" }))
        throw new Error(err.detail || `HTTP ${resp.status}`)
      }
      const data = await resp.json()
      setTokenFingerprint(data.token_fingerprint)
      setAccounts(data.accounts)
      if (data.accounts.length === 1) {
        setSelectedAccount(data.accounts[0].id)
      }
      setStep("select")
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e))
    } finally {
      setLoading(false)
    }
  }, [apiToken])

  // ── Step 2: Load zones when account selected ──
  useEffect(() => {
    if (!selectedAccount) return
    let cancelled = false
    ;(async () => {
      try {
        const resp = await fetch(`/api/v1/cloudflare/zones?account_id=${selectedAccount}`)
        if (!resp.ok) throw new Error("Failed to load zones")
        const data = await resp.json()
        if (!cancelled) setZones(data)
      } catch (e) {
        if (!cancelled) setError(String(e instanceof Error ? e.message : e))
      }
    })()
    return () => { cancelled = true }
  }, [selectedAccount])

  const handleSelectZone = useCallback(() => {
    const zone = zones.find(z => z.id === selectedZone)
    if (zone) {
      setSelectedZoneName(zone.name)
      setHostnames([`omnisight.${zone.name}`, `api.omnisight.${zone.name}`])
    }
    setStep("hostnames")
  }, [selectedZone, zones])

  // ── Step 4: Provision ──
  const handleProvision = useCallback(async () => {
    setStep("provisioning")
    setProvisionEvents([])
    setLoading(true)
    setError(null)

    const evtSource = api.subscribeEvents((evt) => {
      if (evt.event === "cf_tunnel_provision") {
        setProvisionEvents(prev => [...prev, evt.data as unknown as ProvisionEvent])
      }
    })

    try {
      const resp = await fetch("/api/v1/cloudflare/provision", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          account_id: selectedAccount,
          zone_id: selectedZone,
          zone_name: selectedZoneName,
          hostnames,
          tunnel_name: tunnelName,
        }),
      })
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: "Provision failed" }))
        throw new Error(err.detail || `HTTP ${resp.status}`)
      }
      setStep("done")
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e))
    } finally {
      setLoading(false)
      if (evtSource && typeof evtSource === "function") evtSource()
    }
  }, [selectedAccount, selectedZone, selectedZoneName, hostnames, tunnelName])

  // ── Teardown ──
  const handleTeardown = useCallback(async () => {
    if (!confirm("This will delete the tunnel and all DNS records. Continue?")) return
    setLoading(true)
    try {
      const resp = await fetch("/api/v1/cloudflare/tunnel", { method: "DELETE" })
      if (!resp.ok) throw new Error("Teardown failed")
      setExistingStatus(null)
      setShowManage(false)
      resetWizard()
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e))
    } finally {
      setLoading(false)
    }
  }, [resetWizard])

  // ── Rotate Token ──
  const handleRotate = useCallback(async () => {
    const newToken = prompt("Enter new Cloudflare API token:")
    if (!newToken) return
    setLoading(true)
    try {
      const resp = await fetch("/api/v1/cloudflare/rotate-token", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ new_api_token: newToken }),
      })
      if (!resp.ok) throw new Error("Token rotation failed")
      await checkExisting()
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e))
    } finally {
      setLoading(false)
    }
  }, [checkExisting])

  if (!open) return null

  const stepIndex = { token: 0, select: 1, hostnames: 2, review: 3, provisioning: 4, done: 4 }[step]

  const content = (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50" onClick={onClose}>
      <div
        className="bg-[var(--background)] border border-[var(--border)] rounded-lg shadow-xl w-full max-w-lg max-h-[80vh] overflow-y-auto"
        onClick={e => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-[var(--border)]">
          <div className="flex items-center gap-2">
            <Shield size={14} className="text-[var(--neural-blue)]" />
            <span className="font-mono text-[11px] font-bold text-[var(--foreground)]">
              Cloudflare Tunnel Wizard
            </span>
          </div>
          <button onClick={onClose} className="text-[var(--muted-foreground)] hover:text-[var(--foreground)]">
            <X size={14} />
          </button>
        </div>

        <div className="p-4">
          {/* Existing tunnel management */}
          {showManage && existingStatus && (
            <div className="mb-4 p-3 border border-[var(--border)] rounded-md bg-[var(--secondary)]/30">
              <div className="flex items-center justify-between mb-2">
                <span className="font-mono text-[10px] font-bold text-[var(--foreground)]">
                  Active Tunnel: {existingStatus.tunnel_name}
                </span>
                <StatusBadge online={existingStatus.connector_online} />
              </div>
              <div className="font-mono text-[9px] text-[var(--muted-foreground)] mb-2">
                ID: {existingStatus.tunnel_id}
                {existingStatus.hostnames.map(h => (
                  <div key={h} className="flex items-center gap-1 mt-0.5">
                    <Globe size={8} /> {h}
                  </div>
                ))}
              </div>
              <div className="flex gap-2">
                <button
                  onClick={handleRotate}
                  disabled={loading}
                  className="flex items-center gap-1 px-2 py-1 rounded text-[8px] font-mono bg-[var(--neural-blue)]/10 text-[var(--neural-blue)] hover:bg-[var(--neural-blue)]/20"
                >
                  <RefreshCw size={8} /> Rotate Token
                </button>
                <button
                  onClick={handleTeardown}
                  disabled={loading}
                  className="flex items-center gap-1 px-2 py-1 rounded text-[8px] font-mono bg-[var(--critical-red)]/10 text-[var(--critical-red)] hover:bg-[var(--critical-red)]/20"
                >
                  <Trash2 size={8} /> Teardown
                </button>
              </div>
            </div>
          )}

          {!showManage && (
            <>
              <StepIndicator
                current={stepIndex}
                steps={["Token", "Account", "Hostnames", "Review", "Provision"]}
              />

              {error && (
                <div className="mb-3 p-2 rounded bg-[var(--critical-red)]/10 border border-[var(--critical-red)]/30">
                  <div className="flex items-start gap-1.5">
                    <AlertTriangle size={10} className="text-[var(--critical-red)] mt-0.5 shrink-0" />
                    <span className="font-mono text-[9px] text-[var(--critical-red)]">{error}</span>
                  </div>
                </div>
              )}

              {/* Step 1: Token input */}
              {step === "token" && (
                <div className="space-y-3">
                  <div className="font-mono text-[10px] text-[var(--foreground)]">
                    Enter your Cloudflare API Token with the following permissions:
                  </div>
                  <ul className="space-y-0.5">
                    {REQUIRED_SCOPES.map(scope => (
                      <li key={scope} className="font-mono text-[9px] text-[var(--muted-foreground)] flex items-center gap-1">
                        <Check size={8} className="text-[var(--neural-blue)]" /> {scope}
                      </li>
                    ))}
                  </ul>
                  <a
                    href={TOKEN_HELP_URL}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1 font-mono text-[9px] text-[var(--neural-blue)] hover:underline"
                  >
                    <ExternalLink size={8} /> How to create a token
                  </a>
                  <div className="relative">
                    <input
                      type={showToken ? "text" : "password"}
                      value={apiToken}
                      onChange={e => setApiToken(e.target.value)}
                      placeholder="CF API Token"
                      className="w-full px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--background)] font-mono text-[10px] text-[var(--foreground)] pr-8"
                    />
                    <button
                      onClick={() => setShowToken(!showToken)}
                      className="absolute right-2 top-1/2 -translate-y-1/2 text-[var(--muted-foreground)]"
                    >
                      {showToken ? <EyeOff size={10} /> : <Eye size={10} />}
                    </button>
                  </div>
                  <button
                    onClick={handleValidateToken}
                    disabled={loading || !apiToken.trim()}
                    className="w-full py-1.5 rounded font-mono text-[10px] font-bold bg-[var(--neural-blue)] text-white hover:bg-[var(--neural-blue)]/90 disabled:opacity-50"
                  >
                    {loading ? <Loader size={10} className="animate-spin inline mr-1" /> : null}
                    Validate Token
                  </button>
                </div>
              )}

              {/* Step 2: Account / Zone selection */}
              {step === "select" && (
                <div className="space-y-3">
                  <div className="font-mono text-[9px] text-[var(--muted-foreground)]">
                    Token: {tokenFingerprint}
                  </div>
                  <div>
                    <label className="font-mono text-[10px] text-[var(--foreground)] block mb-1">Account</label>
                    <select
                      value={selectedAccount}
                      onChange={e => setSelectedAccount(e.target.value)}
                      className="w-full px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--background)] font-mono text-[10px]"
                    >
                      <option value="">Select account…</option>
                      {accounts.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}
                    </select>
                  </div>
                  {zones.length > 0 && (
                    <div>
                      <label className="font-mono text-[10px] text-[var(--foreground)] block mb-1">Zone (Domain)</label>
                      <select
                        value={selectedZone}
                        onChange={e => setSelectedZone(e.target.value)}
                        className="w-full px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--background)] font-mono text-[10px]"
                      >
                        <option value="">Select zone…</option>
                        {zones.map(z => <option key={z.id} value={z.id}>{z.name}</option>)}
                      </select>
                    </div>
                  )}
                  <button
                    onClick={handleSelectZone}
                    disabled={!selectedZone}
                    className="w-full py-1.5 rounded font-mono text-[10px] font-bold bg-[var(--neural-blue)] text-white hover:bg-[var(--neural-blue)]/90 disabled:opacity-50"
                  >
                    Next
                  </button>
                </div>
              )}

              {/* Step 3: Hostnames */}
              {step === "hostnames" && (
                <div className="space-y-3">
                  <div>
                    <label className="font-mono text-[10px] text-[var(--foreground)] block mb-1">Tunnel Name</label>
                    <input
                      type="text"
                      value={tunnelName}
                      onChange={e => setTunnelName(e.target.value)}
                      className="w-full px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--background)] font-mono text-[10px]"
                    />
                  </div>
                  <div>
                    <label className="font-mono text-[10px] text-[var(--foreground)] block mb-1">Hostnames</label>
                    {hostnames.map((h, i) => (
                      <div key={i} className="flex items-center gap-1 mb-1">
                        <Globe size={8} className="text-[var(--neural-blue)] shrink-0" />
                        <input
                          type="text"
                          value={h}
                          onChange={e => {
                            const next = [...hostnames]
                            next[i] = e.target.value
                            setHostnames(next)
                          }}
                          className="flex-1 px-2 py-1 rounded border border-[var(--border)] bg-[var(--background)] font-mono text-[10px]"
                        />
                        <button
                          onClick={() => setHostnames(hostnames.filter((_, j) => j !== i))}
                          className="text-[var(--critical-red)] hover:text-[var(--critical-red)]/80"
                        >
                          <X size={10} />
                        </button>
                      </div>
                    ))}
                    <button
                      onClick={() => setHostnames([...hostnames, ""])}
                      className="font-mono text-[9px] text-[var(--neural-blue)] hover:underline"
                    >
                      + Add hostname
                    </button>
                  </div>
                  <div className="flex gap-2">
                    <button
                      onClick={() => setStep("select")}
                      className="flex-1 py-1.5 rounded font-mono text-[10px] border border-[var(--border)] hover:bg-[var(--secondary)]"
                    >
                      Back
                    </button>
                    <button
                      onClick={() => setStep("review")}
                      disabled={hostnames.length === 0 || !tunnelName.trim()}
                      className="flex-1 py-1.5 rounded font-mono text-[10px] font-bold bg-[var(--neural-blue)] text-white hover:bg-[var(--neural-blue)]/90 disabled:opacity-50"
                    >
                      Review
                    </button>
                  </div>
                </div>
              )}

              {/* Step 4: Review */}
              {step === "review" && (
                <div className="space-y-3">
                  <div className="p-3 rounded border border-[var(--border)] bg-[var(--secondary)]/30 space-y-1.5">
                    <div className="font-mono text-[9px]">
                      <span className="text-[var(--muted-foreground)]">Account:</span>{" "}
                      <span className="text-[var(--foreground)]">{accounts.find(a => a.id === selectedAccount)?.name}</span>
                    </div>
                    <div className="font-mono text-[9px]">
                      <span className="text-[var(--muted-foreground)]">Zone:</span>{" "}
                      <span className="text-[var(--foreground)]">{selectedZoneName}</span>
                    </div>
                    <div className="font-mono text-[9px]">
                      <span className="text-[var(--muted-foreground)]">Tunnel:</span>{" "}
                      <span className="text-[var(--foreground)]">{tunnelName}</span>
                    </div>
                    <div className="font-mono text-[9px]">
                      <span className="text-[var(--muted-foreground)]">Hostnames:</span>
                      {hostnames.map(h => (
                        <div key={h} className="ml-2 flex items-center gap-1 text-[var(--foreground)]">
                          <Link2 size={8} /> {h}
                        </div>
                      ))}
                    </div>
                    <div className="font-mono text-[9px]">
                      <span className="text-[var(--muted-foreground)]">Ingress:</span>{" "}
                      <span className="text-[var(--foreground)]">http://localhost:8000</span>
                    </div>
                  </div>
                  <div className="flex gap-2">
                    <button
                      onClick={() => setStep("hostnames")}
                      className="flex-1 py-1.5 rounded font-mono text-[10px] border border-[var(--border)] hover:bg-[var(--secondary)]"
                    >
                      Back
                    </button>
                    <button
                      onClick={handleProvision}
                      className="flex-1 py-1.5 rounded font-mono text-[10px] font-bold bg-[var(--validation-emerald)] text-white hover:bg-[var(--validation-emerald)]/90"
                    >
                      <Server size={10} className="inline mr-1" />
                      Provision
                    </button>
                  </div>
                </div>
              )}

              {/* Step 5: Provisioning progress */}
              {step === "provisioning" && (
                <div className="space-y-2">
                  <div className="font-mono text-[10px] text-[var(--foreground)] flex items-center gap-1">
                    <Loader size={10} className="animate-spin text-[var(--neural-blue)]" />
                    Provisioning tunnel…
                  </div>
                  {provisionEvents.map((evt, i) => (
                    <div key={i} className="flex items-center gap-1.5 font-mono text-[9px]">
                      {evt.status === "done" && <Check size={8} className="text-[var(--validation-emerald)]" />}
                      {evt.status === "in_progress" && <Loader size={8} className="animate-spin text-[var(--neural-blue)]" />}
                      {evt.status === "failed" && <AlertTriangle size={8} className="text-[var(--critical-red)]" />}
                      {evt.status === "warn" && <AlertTriangle size={8} className="text-[var(--warning-amber)]" />}
                      <span className="text-[var(--muted-foreground)]">{evt.step}:</span>
                      <span className="text-[var(--foreground)]">{evt.detail}</span>
                    </div>
                  ))}
                </div>
              )}

              {/* Step 5: Done */}
              {step === "done" && (
                <div className="space-y-3 text-center">
                  <div className="flex justify-center">
                    <div className="w-10 h-10 rounded-full bg-[var(--validation-emerald)]/10 flex items-center justify-center">
                      <Check size={20} className="text-[var(--validation-emerald)]" />
                    </div>
                  </div>
                  <div className="font-mono text-[11px] font-bold text-[var(--foreground)]">
                    Tunnel Provisioned Successfully
                  </div>
                  <div className="font-mono text-[9px] text-[var(--muted-foreground)]">
                    Your OmniSight instance is now accessible via:
                  </div>
                  {hostnames.map(h => (
                    <div key={h} className="font-mono text-[10px] text-[var(--neural-blue)]">
                      https://{h}
                    </div>
                  ))}
                  <button
                    onClick={onClose}
                    className="w-full py-1.5 rounded font-mono text-[10px] font-bold bg-[var(--neural-blue)] text-white hover:bg-[var(--neural-blue)]/90"
                  >
                    Close
                  </button>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )

  return typeof document !== "undefined" ? createPortal(content, document.body) : null
}
