"use client"

/**
 * OP-2511 — Guild Hall recruit modal.
 *
 * Owns the browser form for creating a DB-backed character definition.
 * The caller supplies the API submit function and refresh wiring so the
 * modal stays presentational around validation, pending state, and errors.
 */

import { Loader2, UserPlus } from "lucide-react"
import type { FormEvent, ReactElement } from "react"
import { useCallback, useMemo, useState } from "react"

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { ApiError, type RecruitCharacterRequest } from "@/lib/api"
import { cn } from "@/lib/utils"

import type { AgentGuild } from "./CharacterCard"

export interface RecruitModalProps {
  open: boolean
  guilds: readonly AgentGuild[]
  onClose: () => void
  onRecruit: (payload: RecruitCharacterRequest) => Promise<void>
  className?: string
}

const BRAIN_OPTIONS: readonly { value: string; label: string }[] = [
  { value: "subscription-claude", label: "Claude" },
  { value: "codex", label: "Codex" },
  { value: "gemini", label: "Gemini" },
  { value: "grok", label: "Grok" },
]

const TIER_OPTIONS: readonly RecruitCharacterRequest["max_tier"][] = [
  "S",
  "M",
  "L",
  "X",
]

const GUILD_LABEL: Record<AgentGuild, string> = {
  backend: "Backend",
  frontend: "Frontend",
  security: "Security",
  devops: "DevOps",
  data: "Data",
  mobile: "Mobile",
  embedded: "Embedded",
  generalist: "Generalist",
}

function slugFromName(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .replace(/-{2,}/g, "-")
}

function messageFromError(err: unknown): string {
  if (err instanceof ApiError) {
    const detail = err.parsed?.detail
    if (typeof detail === "string" && detail.trim()) return detail
    return err.message
  }
  if (err instanceof Error) return err.message
  return String(err)
}

export function RecruitModal({
  open,
  guilds,
  onClose,
  onRecruit,
  className,
}: RecruitModalProps): ReactElement | null {
  const [displayName, setDisplayName] = useState("")
  const [slug, setSlug] = useState("")
  const [slugEdited, setSlugEdited] = useState(false)
  const [brain, setBrain] = useState(BRAIN_OPTIONS[0].value)
  const [guild, setGuild] = useState<AgentGuild>(guilds[0] ?? "generalist")
  const [maxTier, setMaxTier] =
    useState<RecruitCharacterRequest["max_tier"]>("M")
  const [blurb, setBlurb] = useState("")
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const resetForm = useCallback(() => {
    setDisplayName("")
    setSlug("")
    setSlugEdited(false)
    setBrain(BRAIN_OPTIONS[0].value)
    setGuild(guilds[0] ?? "generalist")
    setMaxTier("M")
    setBlurb("")
    setSubmitting(false)
    setError(null)
  }, [guilds])

  const guildOptions = useMemo(
    () => guilds.length > 0 ? guilds : (["generalist"] as const),
    [guilds],
  )

  const handleOpenChange = useCallback(
    (next: boolean) => {
      if (!next && !submitting) {
        resetForm()
        onClose()
      }
    },
    [onClose, resetForm, submitting],
  )

  const handleCancel = useCallback(
    () => {
      if (submitting) return
      resetForm()
      onClose()
    },
    [onClose, resetForm, submitting],
  )

  const handleDisplayNameChange = useCallback(
    (value: string) => {
      setDisplayName(value)
      if (!slugEdited) setSlug(slugFromName(value))
    },
    [slugEdited],
  )

  const handleSlugChange = useCallback((value: string) => {
    setSlugEdited(true)
    setSlug(slugFromName(value))
  }, [])

  const handleSubmit = useCallback(
    async (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault()
      const cleanDisplayName = displayName.trim()
      const cleanSlug = slugFromName(slug)
      if (!cleanDisplayName) {
        setError("display_name is required")
        return
      }
      if (!cleanSlug) {
        setError("slug must match ^[a-z][a-z0-9-]*$")
        return
      }
      setSubmitting(true)
      setError(null)
      try {
        await onRecruit({
          display_name: cleanDisplayName,
          slug: cleanSlug,
          brain,
          guild,
          max_tier: maxTier,
          blurb: blurb.trim(),
        })
        resetForm()
      } catch (err) {
        setError(messageFromError(err))
      } finally {
        setSubmitting(false)
      }
    },
    [brain, blurb, displayName, guild, maxTier, onRecruit, resetForm, slug],
  )

  if (!open) return null

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        className={cn(
          "max-h-[calc(100vh-2rem)] max-w-xl overflow-y-auto",
          className,
        )}
        data-testid="recruit-modal"
      >
        <DialogHeader>
          <DialogTitle
            className="flex items-center gap-2 font-mono text-sm"
            data-testid="recruit-modal-title"
          >
            <UserPlus className="size-4 text-emerald-500" aria-hidden="true" />
            Recruit character
          </DialogTitle>
          <DialogDescription className="font-mono text-[11px] text-muted-foreground">
            Create a Lv1 character definition for the Guild Hall roster.
          </DialogDescription>
        </DialogHeader>

        <form
          className="space-y-4"
          onSubmit={handleSubmit}
          data-testid="recruit-modal-form"
        >
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="recruit-display-name">Display name</Label>
              <Input
                id="recruit-display-name"
                value={displayName}
                onChange={(event) => handleDisplayNameChange(event.target.value)}
                disabled={submitting}
                autoComplete="off"
                data-testid="recruit-display-name"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="recruit-slug">Slug</Label>
              <Input
                id="recruit-slug"
                value={slug}
                onChange={(event) => handleSlugChange(event.target.value)}
                disabled={submitting}
                autoComplete="off"
                className="font-mono"
                data-testid="recruit-slug"
              />
            </div>
          </div>

          <div className="grid gap-3 sm:grid-cols-3">
            <div className="space-y-1.5">
              <Label htmlFor="recruit-brain">Brain</Label>
              <Select value={brain} onValueChange={setBrain} disabled={submitting}>
                <SelectTrigger
                  id="recruit-brain"
                  className="w-full"
                  data-testid="recruit-brain"
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {BRAIN_OPTIONS.map((option) => (
                    <SelectItem key={option.value} value={option.value}>
                      {option.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="recruit-guild">Guild</Label>
              <Select
                value={guild}
                onValueChange={(value) => setGuild(value as AgentGuild)}
                disabled={submitting}
              >
                <SelectTrigger
                  id="recruit-guild"
                  className="w-full"
                  data-testid="recruit-guild"
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {guildOptions.map((option) => (
                    <SelectItem key={option} value={option}>
                      {GUILD_LABEL[option] ?? option}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="recruit-max-tier">Tier ceiling</Label>
              <Select
                value={maxTier}
                onValueChange={(value) =>
                  setMaxTier(value as RecruitCharacterRequest["max_tier"])
                }
                disabled={submitting}
              >
                <SelectTrigger
                  id="recruit-max-tier"
                  className="w-full"
                  data-testid="recruit-max-tier"
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {TIER_OPTIONS.map((option) => (
                    <SelectItem key={option} value={option}>
                      {option}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="recruit-blurb">Blurb</Label>
            <Textarea
              id="recruit-blurb"
              value={blurb}
              onChange={(event) => setBlurb(event.target.value)}
              disabled={submitting}
              rows={3}
              data-testid="recruit-blurb"
            />
          </div>

          {error ? (
            <div
              className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 font-mono text-xs text-destructive"
              data-testid="recruit-modal-error"
            >
              {error}
            </div>
          ) : null}

          <DialogFooter className="gap-2">
            <button
              type="button"
              onClick={handleCancel}
              disabled={submitting}
              className="inline-flex items-center justify-center rounded border border-border bg-card px-3 py-1.5 font-mono text-xs text-muted-foreground hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
              data-testid="recruit-modal-cancel"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={submitting}
              className="inline-flex items-center justify-center gap-1 rounded border border-emerald-500/55 bg-emerald-500/10 px-3 py-1.5 font-mono text-xs text-emerald-700 hover:bg-emerald-500/20 disabled:cursor-not-allowed disabled:opacity-50 dark:text-emerald-300"
              data-testid="recruit-modal-submit"
            >
              {submitting ? (
                <Loader2 className="size-3 animate-spin" aria-hidden="true" />
              ) : (
                <UserPlus className="size-3" aria-hidden="true" />
              )}
              Recruit
            </button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

export default RecruitModal
