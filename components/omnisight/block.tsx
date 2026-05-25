"use client"

import { useCallback, useMemo, useState } from "react"
import type { HTMLAttributes, ReactNode } from "react"
import { BookOpen, Check, Copy, Link2, Loader2, Play, Share2 } from "lucide-react"
import type { LucideIcon } from "lucide-react"
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuTrigger,
} from "@/components/ui/context-menu"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  createShareableObject,
  executeRunbook,
  saveBlockAsRunbook,
  type CreateShareableObjectRequest,
  type CreateShareableObjectResponse,
  type ExecuteRunbookRequest,
  type ExecuteRunbookResponse,
  type RunbookSummary,
  type SaveBlockAsRunbookRequest,
  type SaveBlockAsRunbookResponse,
} from "@/lib/api"
import { cn } from "@/lib/utils"
import { useFeatureFlagOrDark } from "@/lib/feature-flags-context"

type BlockElement = "div" | "section" | "article" | "aside" | "li" | "button" | "figure"
type BlockTone = "neutral" | "info" | "success" | "warning" | "danger"
export type BlockShareRegion = "command" | "output" | "metadata" | "screenshots"
export type BlockRedactionReason = "secret" | "pii" | "customer_ip" | "ks_envelope"
export type BlockRedactionMask = Record<
  string,
  BlockRedactionReason | BlockRedactionReason[]
>

const SHARE_REGIONS: Array<{ id: BlockShareRegion; label: string }> = [
  { id: "command", label: "Command" },
  { id: "output", label: "Output" },
  { id: "metadata", label: "Metadata" },
  { id: "screenshots", label: "Screenshots" },
]

const TONE_CLASS: Record<BlockTone, string> = {
  neutral: "border-[var(--neural-border,rgba(148,163,184,0.25))] bg-white/[0.02]",
  info: "border-[var(--neural-blue,#3b82f6)]/35 bg-[var(--neural-blue,#3b82f6)]/[0.05]",
  success: "border-[var(--validation-emerald,#10b981)]/35 bg-[var(--validation-emerald,#10b981)]/[0.05]",
  warning: "border-[var(--fui-orange,#f59e0b)]/40 bg-[var(--fui-orange,#f59e0b)]/[0.06]",
  danger: "border-[var(--critical-red,#ef4444)]/40 bg-[var(--critical-red,#ef4444)]/[0.06]",
}

export interface BlockProps extends Omit<HTMLAttributes<HTMLElement>, "title"> {
  as?: BlockElement
  // Button-specific attributes forwarded to the polymorphic Element via
  // {...props} when `as="button"` (e.g. bp-fleet-lanes cards). Both optional
  // so div/section/article/etc. consumers are unaffected. HTMLAttributes<
  // HTMLElement> carries neither, so without these the as="button" call-site
  // fails tsc with TS2322 (OP-1688, follow-up to OP-1684's fast-gate unblock).
  type?: "button" | "submit" | "reset"
  disabled?: boolean
  title?: ReactNode
  titleRight?: ReactNode
  icon?: LucideIcon
  tone?: BlockTone
  kind?: string
  status?: string
  headerClassName?: string
  bodyClassName?: string
  blockId?: string
  tenantId?: string
  userId?: string
  projectId?: string
  sessionId?: string
  blockPayload?: Record<string, unknown>
  blockTitleText?: string
  shareRegions?: BlockShareRegion[]
  redactionMask?: BlockRedactionMask
  createShare?: (
    body: CreateShareableObjectRequest,
  ) => Promise<CreateShareableObjectResponse>
  saveAsRunbook?: (
    body: SaveBlockAsRunbookRequest,
  ) => Promise<SaveBlockAsRunbookResponse>
  runRunbook?: (
    name: string,
    body: ExecuteRunbookRequest,
  ) => Promise<ExecuteRunbookResponse>
}

export function Block({
  as = "div",
  title,
  titleRight,
  icon: Icon,
  tone = "neutral",
  kind,
  status,
  className,
  headerClassName,
  bodyClassName,
  blockId,
  tenantId,
  userId,
  projectId,
  sessionId,
  blockPayload,
  blockTitleText,
  shareRegions,
  redactionMask,
  createShare = createShareableObject,
  saveAsRunbook = saveBlockAsRunbook,
  runRunbook = executeRunbook,
  children,
  ...props
}: BlockProps) {
  // OP-1724: gate block addressability + Share/runbook affordances on the
  // public UI rollout flag, resolved from the backend effective-flags
  // contract via <FeatureFlagsProvider>. This replaces the dead
  // process.env path (env vars inlined at build time are always-false in
  // the browser bundle), so the operator now flips this at runtime via the
  // feature-flag registry. Default-OFF: no DB row -> dark. <Block/> is a
  // ubiquitous primitive that is sometimes rendered in isolation (unit
  // tests, stories) outside the app-root provider, so it reads via the
  // non-throwing useFeatureFlagOrDark (dark when no provider) rather than
  // crashing that subtree.
  const blockModelEnabled = useFeatureFlagOrDark("ui.block_model.enabled")
  const hasHeader = Boolean(title || titleRight || Icon)
  const Element = as
  const enabledRegions = useMemo(
    () => new Set<BlockShareRegion>(shareRegions ?? SHARE_REGIONS.map((region) => region.id)),
    [shareRegions],
  )
  const [shareOpen, setShareOpen] = useState(false)
  const [selectedRegions, setSelectedRegions] = useState<Set<BlockShareRegion>>(
    () => new Set(enabledRegions),
  )
  const [shareUrl, setShareUrl] = useState<string | null>(null)
  const [shareError, setShareError] = useState<string | null>(null)
  const [sharing, setSharing] = useState(false)
  const [copied, setCopied] = useState(false)

  const toggleRegion = useCallback((region: BlockShareRegion) => {
    setSelectedRegions((prev) => {
      const next = new Set(prev)
      if (next.has(region)) {
        next.delete(region)
      } else {
        next.add(region)
      }
      return next
    })
  }, [])

  const handleShare = useCallback(async () => {
    if (!blockId || selectedRegions.size === 0) return
    setSharing(true)
    setShareError(null)
    setShareUrl(null)
    try {
      const base = typeof window !== "undefined" ? window.location.origin : ""
      const body: CreateShareableObjectRequest = {
        object_kind: "block",
        object_id: blockId,
        tenant_id: tenantId ?? null,
        visibility: "private",
        regions: Array.from(selectedRegions),
        base_url: base,
      }
      if (redactionMask) body.redaction_mask = redactionMask
      const resp = await createShare(body)
      setShareUrl(resp.permalink_url || resp.url || "")
    } catch (exc) {
      setShareError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setSharing(false)
    }
  }, [blockId, createShare, redactionMask, selectedRegions, tenantId])

  const handleCopyShareUrl = useCallback(async () => {
    if (!shareUrl) return
    await navigator.clipboard.writeText(shareUrl)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }, [shareUrl])

  // ─── WP.8: Save Block as Runbook + parameter-prompt re-execute ───
  const [runbookOpen, setRunbookOpen] = useState(false)
  const [runbookSaving, setRunbookSaving] = useState(false)
  const [savedRunbook, setSavedRunbook] = useState<RunbookSummary | null>(null)
  const [runbookError, setRunbookError] = useState<string | null>(null)
  const [runbookYaml, setRunbookYaml] = useState<string>("")
  const [runbookName, setRunbookName] = useState<string>("")
  const [runbookDescription, setRunbookDescription] = useState<string>("")
  const [paramValues, setParamValues] = useState<Record<string, string>>({})
  const [running, setRunning] = useState(false)
  const [runResult, setRunResult] = useState<ExecuteRunbookResponse | null>(null)

  const handleSaveAsRunbook = useCallback(async () => {
    if (!blockId || !tenantId || !kind) return
    setRunbookSaving(true)
    setRunbookError(null)
    setSavedRunbook(null)
    setRunResult(null)
    try {
      const titleText = blockTitleText ?? (typeof title === "string" ? title : "")
      const resp = await saveAsRunbook({
        block: {
          block_id: blockId,
          tenant_id: tenantId,
          user_id: userId ?? null,
          project_id: projectId ?? null,
          session_id: sessionId ?? null,
          kind,
          status: status ?? "completed",
          title: titleText,
          payload: blockPayload ?? {},
        },
        name: runbookName || null,
        description: runbookDescription || null,
      })
      setSavedRunbook(resp.runbook)
      setRunbookYaml(resp.yaml)
      // Pre-fill param inputs with the runbook's declared defaults so the
      // operator only has to override placeholders the synthesizer inferred.
      const seed: Record<string, string> = {}
      for (const p of resp.runbook.params) {
        seed[p.name] = p.default == null ? "" : String(p.default)
      }
      setParamValues(seed)
    } catch (exc) {
      setRunbookError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setRunbookSaving(false)
    }
  }, [
    blockId,
    blockPayload,
    blockTitleText,
    kind,
    projectId,
    runbookDescription,
    runbookName,
    saveAsRunbook,
    sessionId,
    status,
    tenantId,
    title,
    userId,
  ])

  const handleExecuteRunbook = useCallback(async () => {
    if (!savedRunbook || !tenantId) return
    setRunning(true)
    setRunbookError(null)
    setRunResult(null)
    try {
      const resp = await runRunbook(savedRunbook.name, {
        params: { ...paramValues },
        tenant_id: tenantId,
        user_id: userId ?? null,
        project_id: projectId ?? null,
        session_id: sessionId ?? null,
        parent_block_id: blockId ?? null,
      })
      setRunResult(resp)
    } catch (exc) {
      setRunbookError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setRunning(false)
    }
  }, [
    blockId,
    paramValues,
    projectId,
    runRunbook,
    savedRunbook,
    sessionId,
    tenantId,
    userId,
  ])

  const block = (
    <Element
      data-block-id={blockModelEnabled ? blockId : undefined}
      data-block-kind={blockModelEnabled ? kind : undefined}
      data-block-status={blockModelEnabled ? status : undefined}
      className={cn(
        "flex min-w-0 flex-col gap-1.5 rounded-sm border p-2",
        TONE_CLASS[tone],
        className,
      )}
      {...props}
    >
      {hasHeader && (
        <div
          className={cn(
            "flex items-center justify-between gap-2 font-mono text-[10px] tracking-[0.18em] text-[var(--muted-foreground,#94a3b8)]",
            headerClassName,
          )}
        >
          <div className="flex min-w-0 items-center gap-1">
            {Icon && <Icon className="h-3 w-3 shrink-0" aria-hidden />}
            {title && <span className="min-w-0 truncate">{title}</span>}
          </div>
          {titleRight && <div className="shrink-0">{titleRight}</div>}
        </div>
      )}
      {bodyClassName ? <div className={bodyClassName}>{children}</div> : children}
    </Element>
  )

  if (!blockModelEnabled || !blockId) return block

  return (
    <>
      <ContextMenu>
        <ContextMenuTrigger asChild>{block}</ContextMenuTrigger>
        <ContextMenuContent className="w-44">
          <ContextMenuItem onSelect={() => setShareOpen(true)}>
            <Share2 className="mr-2 h-3.5 w-3.5" aria-hidden />
            Share
          </ContextMenuItem>
          <ContextMenuItem
            onSelect={() => setRunbookOpen(true)}
            data-testid="block-save-as-runbook"
          >
            <BookOpen className="mr-2 h-3.5 w-3.5" aria-hidden />
            Save as Runbook
          </ContextMenuItem>
        </ContextMenuContent>
      </ContextMenu>
      <Dialog open={shareOpen} onOpenChange={setShareOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Share Block</DialogTitle>
            <DialogDescription className="sr-only">
              Select block regions to include in the permalink.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-2" data-testid="block-share-region-list">
            {SHARE_REGIONS.map((region) => {
              const disabled = !enabledRegions.has(region.id)
              return (
                <label
                  key={region.id}
                  className={cn(
                    "flex items-center gap-2 rounded-sm border border-[var(--border)] px-2 py-1.5 font-mono text-[11px]",
                    disabled && "opacity-45",
                  )}
                >
                  <Checkbox
                    checked={selectedRegions.has(region.id)}
                    disabled={disabled || sharing}
                    aria-label={`Share ${region.label}`}
                    onCheckedChange={() => toggleRegion(region.id)}
                  />
                  <span>{region.label}</span>
                </label>
              )
            })}
          </div>

          {shareError && (
            <div
              className="rounded-sm border border-[var(--destructive)]/40 bg-[var(--destructive)]/10 px-2 py-1.5 font-mono text-[11px] text-[var(--destructive)]"
              data-testid="block-share-error"
            >
              {shareError}
            </div>
          )}

          {shareUrl && (
            <div className="flex items-center gap-2 rounded-sm border border-[var(--border)] px-2 py-1.5">
              <Link2 className="h-3.5 w-3.5 shrink-0 text-[var(--muted-foreground)]" aria-hidden />
              <span
                className="min-w-0 flex-1 truncate font-mono text-[11px]"
                title={shareUrl}
                data-testid="block-share-url"
              >
                {shareUrl}
              </span>
              <button
                type="button"
                className="shrink-0 text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
                onClick={handleCopyShareUrl}
                aria-label="Copy block share URL"
              >
                {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
              </button>
            </div>
          )}

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setShareOpen(false)}
              disabled={sharing}
            >
              Close
            </Button>
            <Button
              type="button"
              onClick={handleShare}
              disabled={sharing || selectedRegions.size === 0}
              data-testid="block-share-create"
            >
              {sharing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Share2 className="h-4 w-4" />}
              Create permalink
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={runbookOpen} onOpenChange={setRunbookOpen}>
        <DialogContent
          className="sm:max-w-lg"
          data-testid="block-save-as-runbook-dialog"
        >
          <DialogHeader>
            <DialogTitle>Save Block as Runbook</DialogTitle>
            <DialogDescription className="sr-only">
              Synthesize a runbook from this block. Inferred parameters
              can be edited at execution time.
            </DialogDescription>
          </DialogHeader>

          {!savedRunbook && (
            <div className="space-y-2">
              <label className="flex flex-col gap-1 font-mono text-[11px]">
                <span>Name (optional)</span>
                <input
                  type="text"
                  className="rounded-sm border border-[var(--border)] bg-transparent px-2 py-1"
                  value={runbookName}
                  onChange={(e) => setRunbookName(e.target.value)}
                  placeholder="auto from block title"
                  data-testid="runbook-name-input"
                />
              </label>
              <label className="flex flex-col gap-1 font-mono text-[11px]">
                <span>Description (optional)</span>
                <input
                  type="text"
                  className="rounded-sm border border-[var(--border)] bg-transparent px-2 py-1"
                  value={runbookDescription}
                  onChange={(e) => setRunbookDescription(e.target.value)}
                  placeholder="auto from block title"
                />
              </label>
            </div>
          )}

          {savedRunbook && (
            <div className="space-y-2">
              <div
                className="rounded-sm border border-[var(--border)] px-2 py-1.5 font-mono text-[11px]"
                data-testid="runbook-saved-summary"
              >
                <div>
                  <span className="text-[var(--muted-foreground)]">name: </span>
                  <span>{savedRunbook.name}</span>
                </div>
                <div className="truncate">
                  <span className="text-[var(--muted-foreground)]">source: </span>
                  <span>{savedRunbook.source_url}</span>
                </div>
              </div>
              {savedRunbook.params.length > 0 && (
                <div
                  className="space-y-1.5"
                  data-testid="runbook-param-prompt"
                >
                  <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--muted-foreground)]">
                    Parameters
                  </div>
                  {savedRunbook.params.map((p) => (
                    <label
                      key={p.name}
                      className="flex flex-col gap-1 font-mono text-[11px]"
                    >
                      <span>
                        {p.name}{" "}
                        <span className="text-[var(--muted-foreground)]">
                          ({p.type}
                          {p.required ? ", required" : ""})
                        </span>
                      </span>
                      <input
                        type="text"
                        className="rounded-sm border border-[var(--border)] bg-transparent px-2 py-1"
                        value={paramValues[p.name] ?? ""}
                        onChange={(e) =>
                          setParamValues((prev) => ({
                            ...prev,
                            [p.name]: e.target.value,
                          }))
                        }
                        placeholder={
                          p.description ||
                          (p.default == null ? "" : String(p.default))
                        }
                        data-testid={`runbook-param-${p.name}`}
                      />
                    </label>
                  ))}
                </div>
              )}
              {runbookYaml && (
                <details
                  className="rounded-sm border border-[var(--border)]"
                  data-testid="runbook-yaml-preview"
                >
                  <summary className="cursor-pointer px-2 py-1 font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--muted-foreground)]">
                    YAML preview
                  </summary>
                  <pre className="overflow-x-auto px-2 py-1 font-mono text-[10px]">
                    {runbookYaml}
                  </pre>
                </details>
              )}
            </div>
          )}

          {runbookError && (
            <div
              className="rounded-sm border border-[var(--destructive)]/40 bg-[var(--destructive)]/10 px-2 py-1.5 font-mono text-[11px] text-[var(--destructive)]"
              data-testid="runbook-error"
            >
              {runbookError}
            </div>
          )}

          {runResult && (
            <div
              className="rounded-sm border border-[var(--validation-emerald,#10b981)]/40 bg-[var(--validation-emerald,#10b981)]/10 px-2 py-1.5 font-mono text-[11px]"
              data-testid="runbook-run-result"
            >
              Produced {runResult.blocks.length} block
              {runResult.blocks.length === 1 ? "" : "s"} chained from {blockId}.
            </div>
          )}

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setRunbookOpen(false)}
              disabled={runbookSaving || running}
            >
              Close
            </Button>
            {!savedRunbook && (
              <Button
                type="button"
                onClick={handleSaveAsRunbook}
                disabled={runbookSaving || !blockId || !tenantId || !kind}
                data-testid="runbook-save-button"
              >
                {runbookSaving ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <BookOpen className="h-4 w-4" />
                )}
                Save runbook
              </Button>
            )}
            {savedRunbook && (
              <Button
                type="button"
                onClick={handleExecuteRunbook}
                disabled={running}
                data-testid="runbook-execute-button"
              >
                {running ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <Play className="h-4 w-4" />
                )}
                Execute
              </Button>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
