"use client"

/**
 * U6-6 L3c — Settings → Memory: Sora's per-user persistent-memory consent
 * surface (frozen design §2.F).
 *
 * Sora can remember small, non-behavioral facts about how YOU like to work
 * (a preferred IPC, your timezone, a repo's build standard). Nothing is ever
 * remembered without your explicit confirmation, and memory can NEVER change
 * how the system behaves — it is data the assistant reads, never authority.
 *
 * The flow this page drives (backend enforces every gate; the UI only
 * surfaces it):
 *   1. **Propose** — you state a fact. It runs the closed-schema + safety
 *      gate on the server and lands QUARANTINED (not live). An authority /
 *      policy value is UNREPRESENTABLE (a 422 from the gate).
 *   2. **Pending → Confirm** — you review the EXACT bytes that would be
 *      injected and confirm one at a time. A SENSITIVE fact needs the
 *      explicit acknowledgment checkbox (the server returns 428 otherwise).
 *   3. **Live → Revoke** — a confirmed fact is shown; revoke removes it from
 *      the injectable set.
 *   4. **Erase all** — crypto-shred every memory (irreversible), behind a
 *      typed-confirmation dialog.
 *
 * Injection itself is a separate operator flag (`OMNISIGHT_SORA_L3_READ`);
 * confirming a fact here only makes it eligible, it does not turn injection
 * on. Identity is entirely server-side — these calls carry no tenant/user.
 *
 * Module-global state audit (per implement_phase_step.md SOP §1): pure
 * browser component; all state is React `useState`. Reads `auth.user` only
 * for the sign-in gate; every backend call goes through `lib/api.ts`.
 */

import { useCallback, useEffect, useState } from "react"
import Link from "next/link"
import {
  AlertTriangle,
  ArrowLeft,
  Brain,
  Check,
  Loader2,
  Plus,
  ShieldQuestion,
  Trash2,
  X,
} from "lucide-react"

import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Toaster } from "@/components/ui/toaster"
import { toast } from "@/hooks/use-toast"
import { useAuth } from "@/lib/auth-context"
import {
  type ApiError,
  type MemoryFactView,
  type PendingMemoryView,
  MEMORY_PREDICATES,
  confirmMemory,
  discardMemory,
  eraseAllMemories,
  listMemories,
  listPendingMemories,
  proposeMemory,
  revokeMemory,
} from "@/lib/api"

const FACT_TYPES = ["preference", "profile", "project_context"] as const
type FactType = (typeof FACT_TYPES)[number]

const FACT_TYPE_COPY: Record<FactType, string> = {
  preference: "偏好 — 你喜歡怎麼做事",
  profile: "個人資料 — 穩定的個人屬性",
  project_context: "專案脈絡 — 某個 repo 的既定標準",
}

function _errMessage(e: unknown): string {
  const err = e as Partial<ApiError> & { message?: string }
  // Prefer the backend's structured `detail` (422 reject reason / 428
  // sensitive-ack / 429 rate limit) over the raw "API 4xx: {json}" message.
  const detail = err?.parsed?.detail
  if (typeof detail === "string" && detail) return detail
  return err?.message || "操作失敗，請稍後再試"
}

export default function MemorySettingsPage() {
  const auth = useAuth()
  const [live, setLive] = useState<MemoryFactView[]>([])
  const [pending, setPending] = useState<PendingMemoryView[]>([])
  const [loading, setLoading] = useState(true)
  const [busyId, setBusyId] = useState<string | null>(null)
  // §2.F: which sensitive pending row is mid-acknowledgment (an explicit,
  // non-default second gesture before its confirm is allowed).
  const [ackId, setAckId] = useState<string | null>(null)

  // propose form
  const [factType, setFactType] = useState<FactType>("preference")
  const [predicate, setPredicate] = useState<string>(MEMORY_PREDICATES.preference[0])
  const [value, setValue] = useState("")
  const [sensitive, setSensitive] = useState(false)
  const [proposing, setProposing] = useState(false)

  // erase dialog
  const [eraseOpen, setEraseOpen] = useState(false)
  const [eraseConfirmText, setEraseConfirmText] = useState("")
  const [erasing, setErasing] = useState(false)

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const [l, p] = await Promise.all([listMemories(), listPendingMemories()])
      setLive(l.memories)
      setPending(p.pending)
    } catch (e) {
      toast({ title: "無法載入記憶", description: _errMessage(e), variant: "destructive" })
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (auth.user) void refresh()
  }, [auth.user, refresh])

  // keep predicate valid when fact_type changes
  useEffect(() => {
    const opts = MEMORY_PREDICATES[factType] ?? []
    if (opts.length && !opts.includes(predicate)) setPredicate(opts[0])
  }, [factType, predicate])

  const onPropose = useCallback(async () => {
    if (!value.trim()) return
    setProposing(true)
    try {
      await proposeMemory({
        fact_type: factType,
        predicate,
        value: value.trim(),
        declared_sensitivity: sensitive ? "sensitive" : "normal",
      })
      setValue("")
      setSensitive(false)
      toast({ title: "已提案", description: "已加入待確認清單，請在下方確認後才會生效。" })
      await refresh()
    } catch (e) {
      // 422 = the closed schema/safety gate rejected it (e.g. an authority value)
      toast({ title: "提案被拒", description: _errMessage(e), variant: "destructive" })
    } finally {
      setProposing(false)
    }
  }, [factType, predicate, value, sensitive, refresh])

  // §2.F: a sensitive candidate is NOT auto-acknowledged. The first "確認"
  // click on a sensitive row only OPENS the acknowledgment (setAckId); the
  // ack + confirm is a deliberate SECOND action (onConfirm called with
  // ack=true). A normal candidate confirms in one click.
  const onConfirm = useCallback(async (row: PendingMemoryView, ack: boolean) => {
    setBusyId(row.fact_id)
    try {
      await confirmMemory(row.fact_id, { acknowledgeSensitive: ack })
      setAckId(null)
      toast({ title: "已確認", description: "這條記憶現在會被 Sora 使用（在注入開啟時）。" })
      await refresh()
    } catch (e) {
      toast({ title: "確認失敗", description: _errMessage(e), variant: "destructive" })
    } finally {
      setBusyId(null)
    }
  }, [refresh])

  const onDiscard = useCallback(async (row: PendingMemoryView) => {
    setBusyId(row.fact_id)
    try {
      await discardMemory(row.fact_id)
      setAckId(null)
      toast({ title: "已丟棄", description: "這個候選已被丟棄，不會被記住。" })
      await refresh()
    } catch (e) {
      toast({ title: "丟棄失敗", description: _errMessage(e), variant: "destructive" })
    } finally {
      setBusyId(null)
    }
  }, [refresh])

  const onRevoke = useCallback(async (row: MemoryFactView) => {
    setBusyId(row.fact_id)
    try {
      await revokeMemory(row.fact_id)
      toast({ title: "已撤銷", description: "這條記憶已從可注入集合中移除。" })
      await refresh()
    } catch (e) {
      toast({ title: "撤銷失敗", description: _errMessage(e), variant: "destructive" })
    } finally {
      setBusyId(null)
    }
  }, [refresh])

  const onErase = useCallback(async () => {
    setErasing(true)
    try {
      const r = await eraseAllMemories()
      toast({ title: "已全部刪除", description: `已 crypto-shred ${r.facts} 條記憶（不可復原）。` })
      setEraseOpen(false)
      setEraseConfirmText("")
      await refresh()
    } catch (e) {
      toast({ title: "刪除失敗", description: _errMessage(e), variant: "destructive" })
    } finally {
      setErasing(false)
    }
  }, [refresh])

  if (!auth.user) {
    return (
      <div className="mx-auto max-w-3xl p-6">
        <p className="text-[var(--muted-foreground)]">請先登入以管理記憶設定。</p>
      </div>
    )
  }

  const predicateOptions = MEMORY_PREDICATES[factType] ?? []

  return (
    <div className="mx-auto max-w-3xl p-6 space-y-6">
      <div className="flex items-center gap-3">
        <Link
          href="/settings/account"
          className="text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
          aria-label="返回設定"
        >
          <ArrowLeft size={18} />
        </Link>
        <Brain size={20} className="text-[var(--primary)]" />
        <h1 className="text-xl font-semibold">Sora 的記憶</h1>
      </div>
      <p className="text-sm text-[var(--muted-foreground)]">
        Sora 可以記住一些關於「你偏好怎麼工作」的小事實（例如偏好的 IPC、時區、某個
        repo 的建置標準）。<strong>沒有你的確認，任何東西都不會被記住</strong>，而且記憶
        永遠只是「資料」——它不能改變系統的行為或權限。
      </p>

      {/* ── Propose ─────────────────────────────────────────────── */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <Plus size={16} /> 新增一條記憶
          </CardTitle>
          <CardDescription>
            你明確地告訴 Sora 一個事實；它會先通過安全檢查並進入「待確認」，不會立即生效。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1">
              <label className="text-xs text-[var(--muted-foreground)]">類別</label>
              <Select value={factType} onValueChange={(v) => setFactType(v as FactType)}>
                <SelectTrigger data-testid="memory-fact-type">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {FACT_TYPES.map((ft) => (
                    <SelectItem key={ft} value={ft}>{FACT_TYPE_COPY[ft]}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1">
              <label className="text-xs text-[var(--muted-foreground)]">屬性</label>
              <Select value={predicate} onValueChange={setPredicate}>
                <SelectTrigger data-testid="memory-predicate">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {predicateOptions.map((p) => (
                    <SelectItem key={p} value={p}>{p}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>
          <div className="space-y-1">
            <label className="text-xs text-[var(--muted-foreground)]">值</label>
            <Input
              value={value}
              onChange={(e) => setValue(e.target.value)}
              placeholder="例如 named_pipes"
              maxLength={64}
              data-testid="memory-value"
            />
          </div>
          <label className="flex items-center gap-2 text-sm">
            <Checkbox
              checked={sensitive}
              onCheckedChange={(c) => setSensitive(c === true)}
              data-testid="memory-sensitive"
            />
            標記為敏感（確認時會要求額外的確認）
          </label>
          <div className="flex justify-end">
            <Button onClick={onPropose} disabled={proposing || !value.trim()} data-testid="memory-propose">
              {proposing ? <Loader2 size={14} className="animate-spin" /> : <Plus size={14} />}
              提案
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* ── Pending ─────────────────────────────────────────────── */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <ShieldQuestion size={16} /> 待確認
            {pending.length > 0 && <Badge variant="secondary">{pending.length}</Badge>}
          </CardTitle>
          <CardDescription>
            確認前請檢視「將被注入的確切內容」（下方等寬字）。一次確認一條。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-2">
          {loading ? (
            <Loader2 size={16} className="animate-spin text-[var(--muted-foreground)]" />
          ) : pending.length === 0 ? (
            <p className="text-sm text-[var(--muted-foreground)]">沒有待確認的候選。</p>
          ) : (
            pending.map((row) => {
              const isSensitive = row.sensitivity === "sensitive"
              const acking = ackId === row.fact_id
              return (
                <div
                  key={row.fact_id}
                  className="rounded border border-[var(--border)] p-3"
                  data-testid="memory-pending-row"
                >
                  <div className="flex items-center justify-between gap-3">
                    <div className="min-w-0">
                      <code className="block truncate text-sm">{row.rendered}</code>
                      <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-[var(--muted-foreground)]">
                        <span>{row.fact_type}</span>
                        <span>· 來源 {row.source_span}</span>
                        {row.valid_until && <span>· 有效至 {row.valid_until}</span>}
                        <span>· 只是資料，無任何權限</span>
                        {isSensitive && (
                          <Badge variant="destructive" className="text-[10px]">敏感</Badge>
                        )}
                      </div>
                    </div>
                    <div className="flex shrink-0 gap-1">
                      {/* Non-sensitive → one click. Sensitive → first click opens
                          the acknowledgment gate below (no default-confirm). */}
                      <Button
                        size="sm"
                        onClick={() => (isSensitive ? setAckId(acking ? null : row.fact_id) : onConfirm(row, false))}
                        disabled={busyId === row.fact_id}
                        data-testid="memory-confirm"
                      >
                        {busyId === row.fact_id ? <Loader2 size={14} className="animate-spin" /> : <Check size={14} />}
                        {isSensitive ? "確認…" : "確認"}
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => onDiscard(row)}
                        disabled={busyId === row.fact_id}
                        aria-label="丟棄"
                        data-testid="memory-discard"
                      >
                        <X size={14} />
                      </Button>
                    </div>
                  </div>
                  {isSensitive && acking && (
                    <div
                      className="mt-3 space-y-2 rounded bg-[var(--secondary)]/50 p-2"
                      data-testid="memory-sensitive-ack"
                    >
                      <p className="text-xs text-[var(--muted-foreground)]">
                        這條記憶被標記為<strong>敏感</strong>。請明確確認你了解它會被儲存並在注入開啟時被 Sora 讀取。
                      </p>
                      <div className="flex gap-2">
                        <Button
                          size="sm"
                          variant="destructive"
                          onClick={() => onConfirm(row, true)}
                          disabled={busyId === row.fact_id}
                          data-testid="memory-confirm-sensitive"
                        >
                          我了解，確認記住
                        </Button>
                        <Button size="sm" variant="ghost" onClick={() => setAckId(null)}>
                          取消
                        </Button>
                      </div>
                    </div>
                  )}
                </div>
              )
            })
          )}
        </CardContent>
      </Card>

      {/* ── Live ────────────────────────────────────────────────── */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">已生效的記憶</CardTitle>
          <CardDescription>
            這些是 Sora 在注入開啟時會讀到的事實。你可以隨時撤銷。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-2">
          {loading ? (
            <Loader2 size={16} className="animate-spin text-[var(--muted-foreground)]" />
          ) : live.length === 0 ? (
            <p className="text-sm text-[var(--muted-foreground)]">目前沒有已生效的記憶。</p>
          ) : (
            live.map((row) => (
              <div
                key={row.fact_id}
                className="flex items-center justify-between gap-3 rounded border border-[var(--border)] p-3"
                data-testid="memory-live-row"
              >
                <div className="min-w-0">
                  <code className="block truncate text-sm">{row.rendered}</code>
                  <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-[var(--muted-foreground)]">
                    <span>{row.fact_type}</span>
                    <span>· 來源 {row.source_span}</span>
                    {row.valid_until && <span>· 有效至 {row.valid_until}</span>}
                  </div>
                </div>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => onRevoke(row)}
                  disabled={busyId === row.fact_id}
                  data-testid="memory-revoke"
                >
                  {busyId === row.fact_id ? <Loader2 size={14} className="animate-spin" /> : "撤銷"}
                </Button>
              </div>
            ))
          )}
        </CardContent>
      </Card>

      {/* ── Danger zone ─────────────────────────────────────────── */}
      <Card className="border-[var(--destructive)]/40">
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base text-[var(--destructive)]">
            <AlertTriangle size={16} /> 刪除所有記憶
          </CardTitle>
          <CardDescription>
            這會用 crypto-shred 永久刪除你所有的記憶（含待確認與已生效），<strong>不可復原</strong>。
          </CardDescription>
        </CardHeader>
        <CardContent>
          {!eraseOpen ? (
            <Button variant="destructive" onClick={() => setEraseOpen(true)} data-testid="memory-erase-open">
              <Trash2 size={14} /> 刪除全部…
            </Button>
          ) : (
            <div className="space-y-2">
              <p className="text-sm">請輸入 <code>DELETE</code> 以確認：</p>
              <Input
                value={eraseConfirmText}
                onChange={(e) => setEraseConfirmText(e.target.value)}
                placeholder="DELETE"
                data-testid="memory-erase-confirm-input"
              />
              <div className="flex gap-2">
                <Button
                  variant="destructive"
                  disabled={eraseConfirmText !== "DELETE" || erasing}
                  onClick={onErase}
                  data-testid="memory-erase-confirm"
                >
                  {erasing ? <Loader2 size={14} className="animate-spin" /> : <Trash2 size={14} />}
                  永久刪除
                </Button>
                <Button variant="ghost" onClick={() => { setEraseOpen(false); setEraseConfirmText("") }}>
                  取消
                </Button>
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      <Toaster />
    </div>
  )
}
