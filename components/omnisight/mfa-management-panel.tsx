"use client"

import { useCallback, useEffect, useState } from "react"
import { Shield, ShieldCheck, ShieldOff, Key, Copy, RefreshCw, Trash2, Plus, Loader2, AlertCircle, CheckCircle2 } from "lucide-react"
import {
  mfaStatus,
  mfaTotpEnroll,
  mfaTotpConfirm,
  mfaTotpDisable,
  mfaBackupCodesStatus,
  mfaBackupCodesRegenerate,
  mfaWebauthnRegisterBegin,
  mfaWebauthnRegisterComplete,
  mfaWebauthnRemove,
  type MfaMethod,
  type MfaStatusResponse,
} from "@/lib/api"

type Step = "idle" | "totp-enroll" | "totp-confirm" | "webauthn-register"

export function MfaManagementPanel() {
  const [status, setStatus] = useState<MfaStatusResponse | null>(null)
  const [backupStatus, setBackupStatus] = useState<{ total: number; remaining: number } | null>(null)
  const [step, setStep] = useState<Step>("idle")
  const [enrollData, setEnrollData] = useState<{ secret: string; qr_png_b64: string } | null>(null)
  const [totpCode, setTotpCode] = useState("")
  const [backupCodes, setBackupCodes] = useState<string[] | null>(null)
  const [webauthnName, setWebauthnName] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)

  const loadStatus = useCallback(async () => {
    try {
      const [s, b] = await Promise.all([mfaStatus(), mfaBackupCodesStatus()])
      setStatus(s)
      setBackupStatus(b)
    } catch {
      // not critical
    }
  }, [])

  useEffect(() => { void loadStatus() }, [loadStatus])

  const clearMessages = () => { setError(null); setSuccess(null) }

  const handleTotpEnroll = async () => {
    clearMessages()
    setBusy(true)
    try {
      const res = await mfaTotpEnroll()
      setEnrollData({ secret: res.secret, qr_png_b64: res.qr_png_b64 })
      setStep("totp-enroll")
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to start TOTP enrollment")
    } finally {
      setBusy(false)
    }
  }

  const handleTotpConfirm = async () => {
    if (!totpCode.trim()) return
    clearMessages()
    setBusy(true)
    try {
      const res = await mfaTotpConfirm(totpCode.trim())
      setBackupCodes(res.backup_codes)
      setStep("totp-confirm")
      setSuccess("TOTP enrolled successfully")
      await loadStatus()
    } catch (e) {
      setError(e instanceof Error ? e.message : "Invalid TOTP code")
    } finally {
      setBusy(false)
    }
  }

  const handleTotpDisable = async () => {
    clearMessages()
    setBusy(true)
    try {
      await mfaTotpDisable()
      setSuccess("TOTP disabled")
      await loadStatus()
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to disable TOTP")
    } finally {
      setBusy(false)
    }
  }

  const handleRegenerateBackupCodes = async () => {
    clearMessages()
    setBusy(true)
    try {
      const res = await mfaBackupCodesRegenerate()
      setBackupCodes(res.codes)
      setSuccess("Backup codes regenerated")
      await loadStatus()
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to regenerate")
    } finally {
      setBusy(false)
    }
  }

  const handleWebauthnRegister = async () => {
    clearMessages()
    setBusy(true)
    try {
      const options = await mfaWebauthnRegisterBegin(webauthnName)
      const credential = await navigator.credentials.create({
        publicKey: decodePublicKeyOptions(options),
      })
      if (!credential) throw new Error("No credential returned")
      const encoded = encodeRegistrationCredential(credential as PublicKeyCredential)
      await mfaWebauthnRegisterComplete(encoded, webauthnName)
      setSuccess("Security key registered")
      setStep("idle")
      setWebauthnName("")
      await loadStatus()
    } catch (e) {
      setError(e instanceof Error ? e.message : "WebAuthn registration failed")
    } finally {
      setBusy(false)
    }
  }

  const handleWebauthnRemove = async (mfaId: string) => {
    clearMessages()
    setBusy(true)
    try {
      await mfaWebauthnRemove(mfaId)
      setSuccess("Security key removed")
      await loadStatus()
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to remove key")
    } finally {
      setBusy(false)
    }
  }

  const copyBackupCodes = () => {
    if (!backupCodes) return
    navigator.clipboard.writeText(backupCodes.join("\n"))
    setSuccess("Copied to clipboard")
  }

  const totpEnrolled = status?.methods.some(m => m.method === "totp" && m.verified) ?? false
  const webauthnKeys = status?.methods.filter(m => m.method === "webauthn" && m.verified) ?? []

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-2 mb-1">
        <Shield size={16} className="text-[var(--artifact-purple)]" />
        <h2 className="font-mono text-sm font-semibold text-[var(--foreground)]">
          Multi-Factor Authentication
        </h2>
        {status?.has_mfa && (
          <span className="px-1.5 py-0.5 rounded bg-green-500/20 text-green-400 font-mono text-[10px] uppercase">
            Active
          </span>
        )}
      </div>

      {status?.require_mfa && !status.has_mfa && (
        <div className="flex items-start gap-2 p-2 rounded border border-yellow-500/30 bg-yellow-500/10 text-yellow-300 font-mono text-xs">
          <AlertCircle size={14} className="shrink-0 mt-0.5" />
          <span>MFA is required for your role. Please enroll an authenticator.</span>
        </div>
      )}

      {error && (
        <div className="flex items-start gap-2 p-2 rounded border border-[var(--destructive)] bg-[var(--destructive)]/10 text-[var(--destructive)] font-mono text-xs">
          <AlertCircle size={14} className="shrink-0 mt-0.5" />
          <span>{error}</span>
        </div>
      )}

      {success && (
        <div className="flex items-start gap-2 p-2 rounded border border-green-500/30 bg-green-500/10 text-green-400 font-mono text-xs">
          <CheckCircle2 size={14} className="shrink-0 mt-0.5" />
          <span>{success}</span>
        </div>
      )}

      {/* TOTP Section */}
      <div className="border border-[var(--border)] rounded p-3">
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-2">
            <ShieldCheck size={14} className="text-[var(--muted-foreground)]" />
            <span className="font-mono text-xs font-semibold text-[var(--foreground)]">
              Authenticator App (TOTP)
            </span>
          </div>
          {totpEnrolled ? (
            <button
              onClick={handleTotpDisable}
              disabled={busy}
              className="flex items-center gap-1 px-2 py-1 rounded text-[var(--destructive)] hover:bg-[var(--destructive)]/10 font-mono text-[10px]"
            >
              <ShieldOff size={12} /> Disable
            </button>
          ) : (
            <button
              onClick={handleTotpEnroll}
              disabled={busy}
              className="flex items-center gap-1 px-2 py-1 rounded bg-[var(--artifact-purple)] text-white font-mono text-[10px] hover:opacity-90 disabled:opacity-40"
            >
              {busy ? <Loader2 size={12} className="animate-spin" /> : <Plus size={12} />}
              Enable
            </button>
          )}
        </div>
        <p className="font-mono text-[10px] text-[var(--muted-foreground)]">
          {totpEnrolled
            ? "TOTP is active. Use your authenticator app for 2FA codes."
            : "Use an authenticator app like Google Authenticator or Authy."}
        </p>
      </div>

      {/* TOTP Enrollment Flow */}
      {step === "totp-enroll" && enrollData && (
        <div className="border border-[var(--artifact-purple)]/30 rounded p-3 flex flex-col gap-3">
          <p className="font-mono text-xs text-[var(--foreground)]">
            Scan this QR code with your authenticator app:
          </p>
          <div className="flex justify-center">
            <img
              src={`data:image/png;base64,${enrollData.qr_png_b64}`}
              alt="TOTP QR Code"
              className="w-48 h-48 rounded border border-[var(--border)]"
            />
          </div>
          <div className="flex flex-col gap-1">
            <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
              Or enter manually:
            </span>
            <code className="font-mono text-xs bg-[var(--background)] border border-[var(--border)] rounded px-2 py-1 break-all select-all">
              {enrollData.secret}
            </code>
          </div>
          <div className="flex items-end gap-2">
            <label className="flex-1 flex flex-col gap-1">
              <span className="font-mono text-[10px] tracking-wider text-[var(--muted-foreground)]">
                VERIFICATION CODE
              </span>
              <input
                type="text"
                autoComplete="one-time-code"
                autoFocus
                value={totpCode}
                onChange={(e) => setTotpCode(e.target.value)}
                className="px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--background)] font-mono text-sm text-[var(--foreground)] tracking-widest outline-none focus:ring-1 focus-visible:ring-1 focus:ring-[var(--artifact-purple)] focus-visible:ring-[var(--artifact-purple)]"
                placeholder="000000"
                maxLength={8}
              />
            </label>
            <button
              onClick={handleTotpConfirm}
              disabled={busy || !totpCode.trim()}
              className="px-3 py-1.5 rounded bg-[var(--artifact-purple)] text-white font-mono text-xs font-semibold hover:opacity-90 disabled:opacity-40"
            >
              {busy ? <Loader2 size={12} className="animate-spin" /> : "Verify"}
            </button>
            <button
              onClick={() => { setStep("idle"); setEnrollData(null); setTotpCode("") }}
              className="px-3 py-1.5 rounded bg-[var(--secondary)] text-[var(--foreground)] font-mono text-xs hover:opacity-80"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* Backup codes display */}
      {(step === "totp-confirm" || backupCodes) && backupCodes && (
        <div className="border border-yellow-500/30 rounded p-3 flex flex-col gap-2">
          <div className="flex items-center justify-between">
            <span className="font-mono text-xs font-semibold text-yellow-300">
              Backup Codes
            </span>
            <button
              onClick={copyBackupCodes}
              className="flex items-center gap-1 px-2 py-0.5 rounded hover:bg-[var(--secondary)] font-mono text-[10px] text-[var(--muted-foreground)]"
            >
              <Copy size={10} /> Copy
            </button>
          </div>
          <p className="font-mono text-[10px] text-[var(--muted-foreground)]">
            Save these codes in a safe place. Each code can only be used once.
          </p>
          <div className="grid grid-cols-2 gap-1">
            {backupCodes.map((code, i) => (
              <code key={i} className="font-mono text-xs bg-[var(--background)] border border-[var(--border)] rounded px-2 py-0.5 text-center select-all">
                {code}
              </code>
            ))}
          </div>
          <button
            onClick={() => { setBackupCodes(null); setStep("idle") }}
            className="self-end px-2 py-1 rounded bg-[var(--secondary)] text-[var(--foreground)] font-mono text-[10px]"
          >
            Done
          </button>
        </div>
      )}

      {/* Backup codes status */}
      {status?.has_mfa && backupStatus && !backupCodes && (
        <div className="border border-[var(--border)] rounded p-3">
          <div className="flex items-center justify-between mb-1">
            <div className="flex items-center gap-2">
              <Key size={14} className="text-[var(--muted-foreground)]" />
              <span className="font-mono text-xs font-semibold text-[var(--foreground)]">
                Backup Codes
              </span>
            </div>
            <button
              onClick={handleRegenerateBackupCodes}
              disabled={busy}
              className="flex items-center gap-1 px-2 py-1 rounded hover:bg-[var(--secondary)] font-mono text-[10px] text-[var(--muted-foreground)]"
            >
              <RefreshCw size={10} /> Regenerate
            </button>
          </div>
          <p className="font-mono text-[10px] text-[var(--muted-foreground)]">
            {backupStatus.remaining} of {backupStatus.total} codes remaining
          </p>
        </div>
      )}

      {/* WebAuthn Section */}
      <div className="border border-[var(--border)] rounded p-3">
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-2">
            <Key size={14} className="text-[var(--muted-foreground)]" />
            <span className="font-mono text-xs font-semibold text-[var(--foreground)]">
              Security Keys (WebAuthn)
            </span>
          </div>
          <button
            onClick={() => setStep("webauthn-register")}
            disabled={busy}
            className="flex items-center gap-1 px-2 py-1 rounded bg-[var(--artifact-purple)] text-white font-mono text-[10px] hover:opacity-90 disabled:opacity-40"
          >
            <Plus size={12} /> Add Key
          </button>
        </div>

        {webauthnKeys.length === 0 && (
          <p className="font-mono text-[10px] text-[var(--muted-foreground)]">
            No security keys registered. Add a hardware key for phishing-resistant 2FA.
          </p>
        )}

        {webauthnKeys.map(k => (
          <div key={k.id} className="flex items-center justify-between py-1 border-t border-[var(--border)]">
            <div>
              <span className="font-mono text-xs text-[var(--foreground)]">{k.name}</span>
              <span className="ml-2 font-mono text-[10px] text-[var(--muted-foreground)]">
                added {k.created_at}
              </span>
            </div>
            <button
              onClick={() => handleWebauthnRemove(k.id)}
              disabled={busy}
              className="p-1 rounded hover:bg-[var(--destructive)]/10 text-[var(--destructive)]"
            >
              <Trash2 size={12} />
            </button>
          </div>
        ))}
      </div>

      {/* WebAuthn Registration */}
      {step === "webauthn-register" && (
        <div className="border border-[var(--artifact-purple)]/30 rounded p-3 flex flex-col gap-2">
          <label className="flex flex-col gap-1">
            <span className="font-mono text-[10px] tracking-wider text-[var(--muted-foreground)]">
              KEY NAME (optional)
            </span>
            <input
              type="text"
              value={webauthnName}
              onChange={(e) => setWebauthnName(e.target.value)}
              className="px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--background)] font-mono text-sm text-[var(--foreground)] outline-none focus:ring-1 focus-visible:ring-1 focus:ring-[var(--artifact-purple)] focus-visible:ring-[var(--artifact-purple)]"
              placeholder="e.g. YubiKey 5"
            />
          </label>
          <div className="flex gap-2">
            <button
              onClick={handleWebauthnRegister}
              disabled={busy}
              className="px-3 py-1.5 rounded bg-[var(--artifact-purple)] text-white font-mono text-xs font-semibold hover:opacity-90 disabled:opacity-40"
            >
              {busy ? <Loader2 size={12} className="animate-spin" /> : "Register Key"}
            </button>
            <button
              onClick={() => { setStep("idle"); setWebauthnName("") }}
              className="px-3 py-1.5 rounded bg-[var(--secondary)] text-[var(--foreground)] font-mono text-xs hover:opacity-80"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

// ── WebAuthn encoding helpers ──

function base64urlDecode(s: string): ArrayBuffer {
  let base64 = s.replace(/-/g, "+").replace(/_/g, "/")
  while (base64.length % 4 !== 0) base64 += "="
  const binary = atob(base64)
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i)
  return bytes.buffer
}

function base64urlEncode(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf)
  let binary = ""
  for (const b of bytes) binary += String.fromCharCode(b)
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "")
}

function decodePublicKeyOptions(options: Record<string, unknown>): PublicKeyCredentialCreationOptions {
  const o = options as Record<string, any>
  return {
    ...o,
    challenge: base64urlDecode(o.challenge),
    user: {
      ...o.user,
      id: base64urlDecode(o.user.id),
    },
    excludeCredentials: (o.excludeCredentials || []).map((c: any) => ({
      ...c,
      id: base64urlDecode(c.id),
    })),
  } as PublicKeyCredentialCreationOptions
}

function encodeRegistrationCredential(cred: PublicKeyCredential): Record<string, unknown> {
  const response = cred.response as AuthenticatorAttestationResponse
  return {
    id: cred.id,
    rawId: base64urlEncode(cred.rawId),
    type: cred.type,
    response: {
      attestationObject: base64urlEncode(response.attestationObject),
      clientDataJSON: base64urlEncode(response.clientDataJSON),
    },
  }
}
