"use client"

import { useCallback, useMemo, useState } from "react"
import type { HTMLAttributes, ReactNode } from "react"
import { Check, Copy, Link2, Loader2, Share2 } from "lucide-react"
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
  type CreateShareableObjectRequest,
  type CreateShareableObjectResponse,
} from "@/lib/api"
import { cn } from "@/lib/utils"

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
  type?: "button" | "submit" | "reset"
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
  shareRegions?: BlockShareRegion[]
  redactionMask?: BlockRedactionMask
  createShare?: (
    body: CreateShareableObjectRequest,
  ) => Promise<CreateShareableObjectResponse>
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
  shareRegions,
  redactionMask,
  createShare = createShareableObject,
  children,
  ...props
}: BlockProps) {
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

  const block = (
    <Element
      data-block-id={blockId}
      data-block-kind={kind}
      data-block-status={status}
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

  if (!blockId) return block

  return (
    <>
      <ContextMenu>
        <ContextMenuTrigger asChild>{block}</ContextMenuTrigger>
        <ContextMenuContent className="w-44">
          <ContextMenuItem onSelect={() => setShareOpen(true)}>
            <Share2 className="mr-2 h-3.5 w-3.5" aria-hidden />
            Share
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
    </>
  )
}
