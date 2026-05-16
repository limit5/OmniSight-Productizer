"use client"

/**
 * RPG.W17.6 — Party builder modal.
 *
 * Operator-facing surface for composing a new party before the caller
 * POSTs to `/api/v1/agents/parties` (see
 * `backend/routers/agents.py::create_party_endpoint`). Pure
 * presentation: the caller resolves the candidate roster, the synergy
 * matrix, and the submit wiring; the modal owns only the operator
 * review surface and selection validation.
 *
 * Selection contract (matches `backend/agents/party.py` size bounds):
 *   - 2 ≤ selected ≤ 5
 *   - name must be non-empty after trim
 *   - agents already in an active party are disabled (server would
 *     reject with 409 `MemberAlreadyInParty`; surfacing the gate here
 *     keeps the operator from learning the rule via failure)
 *
 * Synergy preview mirrors `synergy_registry.synergy_for_members`:
 * pick the strongest matrix entry whose Guild pair is fully covered
 * by the selected members, ordered by (xp_bonus desc, skill_bonus
 * desc, label asc) for determinism. Renders `<PartyBadge>` so the
 * preview tile is visually identical to the Party Hall display badge.
 */

import { Sparkles, Users, UserPlus, ShieldAlert } from "lucide-react"
import type { ReactElement } from "react"
import { useCallback, useMemo, useState } from "react"

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Badge } from "@/components/ui/badge"
import { Input } from "@/components/ui/input"
import { cn } from "@/lib/utils"

import type { AgentGuild } from "./CharacterCard"
import { PartyBadge, type PartySynergy } from "./PartyBadge"

export const MIN_PARTY_SIZE = 2
export const MAX_PARTY_SIZE = 5

export interface PartyCandidateAgent {
  agentId: string
  displayName: string
  guild: AgentGuild
  portraitUrl?: string | null
  /**
   * When set, the agent is locked out of selection because they
   * already belong to an active party. Mirrors the server-side
   * `MemberInActiveParty` / `MemberAlreadyInParty` rules.
   */
  activePartyName?: string | null
}

export interface PartyBuilderSubmission {
  name: string
  memberAgentIds: string[]
  memberGuilds: Record<string, AgentGuild>
  /** The synergy preview the operator saw at confirm time, or null. */
  synergy: PartySynergy | null
}

export interface PartyBuilderModalProps {
  /** Controls modal visibility — mount/unmount is owned by the caller. */
  open: boolean
  /** Candidate agents the operator may pick from. */
  candidates: readonly PartyCandidateAgent[]
  /** Full synergy matrix from `GET /api/v1/agents/parties/synergies`. */
  synergies: readonly PartySynergy[]
  /**
   * Called once the operator confirms a valid composition. Caller is
   * responsible for POSTing to the backend and closing the modal on
   * success.
   */
  onConfirm: (submission: PartyBuilderSubmission) => void
  /** Called when the operator dismisses (Esc, overlay, Cancel). */
  onClose: () => void
  /**
   * When true the confirm button shows a busy state and the modal
   * stops accepting new submissions — used while the caller awaits
   * the POST response.
   */
  submitting?: boolean
  /**
   * Server-side error message (e.g. 409 from a member who joined
   * another party between the candidate fetch and the submit).
   */
  errorMessage?: string | null
  /**
   * Initial party name pre-fill. Useful when the operator opened the
   * builder from a task context that suggests a name.
   */
  initialName?: string
  className?: string
}

function pickStrongestSynergy(
  guilds: readonly AgentGuild[],
  synergies: readonly PartySynergy[],
): PartySynergy | null {
  const bag = new Set(guilds.map((g) => g.toLowerCase()))
  if (bag.size < 2) return null
  const applicable = synergies.filter((entry) =>
    entry.guilds.every((g) => bag.has(g.toLowerCase())),
  )
  if (applicable.length === 0) return null
  const sorted = applicable.slice().sort((a, b) => {
    if (b.xpBonus !== a.xpBonus) return b.xpBonus - a.xpBonus
    const bSkill = b.skillBonus ?? 0
    const aSkill = a.skillBonus ?? 0
    if (bSkill !== aSkill) return bSkill - aSkill
    return a.label.localeCompare(b.label)
  })
  return sorted[0]
}

function CandidateRow({
  candidate,
  selected,
  disabled,
  onToggle,
}: {
  candidate: PartyCandidateAgent
  selected: boolean
  disabled: boolean
  onToggle: () => void
}): ReactElement {
  const lockedOut = Boolean(candidate.activePartyName)
  const isDisabled = disabled || lockedOut
  return (
    <button
      type="button"
      onClick={onToggle}
      disabled={isDisabled}
      aria-pressed={selected}
      data-testid="party-builder-candidate"
      data-agent-id={candidate.agentId}
      data-guild={candidate.guild}
      data-selected={selected ? "true" : "false"}
      data-locked-out={lockedOut ? "true" : "false"}
      className={cn(
        "flex w-full items-center gap-3 rounded-md border bg-background px-3 py-2 text-left transition",
        selected && "border-violet-500/60 bg-violet-500/10",
        !selected && !isDisabled && "hover:bg-muted",
        isDisabled && "cursor-not-allowed opacity-50",
      )}
    >
      <div
        className="flex size-9 shrink-0 items-center justify-center rounded-full border-2 border-background bg-muted text-xs font-medium uppercase"
        aria-hidden="true"
      >
        {candidate.portraitUrl ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={candidate.portraitUrl}
            alt=""
            className="size-full rounded-full object-cover"
          />
        ) : (
          <span>{initialsOf(candidate.displayName, candidate.agentId)}</span>
        )}
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate text-sm font-semibold">
            {candidate.displayName}
          </span>
          <Badge
            variant="secondary"
            className="h-5 px-1.5 text-[10px] uppercase"
            data-testid="party-builder-candidate-guild"
          >
            {candidate.guild}
          </Badge>
        </div>
        <p className="mt-0.5 truncate font-mono text-[11px] text-muted-foreground">
          {candidate.agentId}
        </p>
        {lockedOut ? (
          <p
            className="mt-1 inline-flex items-center gap-1 font-mono text-[10px] text-amber-700 dark:text-amber-300"
            data-testid="party-builder-candidate-locked"
          >
            <ShieldAlert className="size-3" aria-hidden="true" />
            In party: {candidate.activePartyName}
          </p>
        ) : null}
      </div>
      <span
        className={cn(
          "shrink-0 rounded-full border px-2 py-0.5 font-mono text-[10px] uppercase",
          selected
            ? "border-violet-500/60 bg-violet-500/15 text-violet-700 dark:text-violet-300"
            : "border-border bg-muted/40 text-muted-foreground",
        )}
      >
        {selected ? "Picked" : "Pick"}
      </span>
    </button>
  )
}

function initialsOf(displayName: string, agentId: string): string {
  const source = displayName?.trim() || agentId
  const parts = source.split(/[\s\-_]+/).filter(Boolean)
  if (parts.length === 0) return "?"
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase()
  return (parts[0][0] + parts[1][0]).toUpperCase()
}

export function PartyBuilderModal({
  open,
  candidates,
  synergies,
  onConfirm,
  onClose,
  submitting,
  errorMessage,
  initialName,
  className,
}: PartyBuilderModalProps): ReactElement | null {
  const [name, setName] = useState<string>(initialName ?? "")
  const [selectedIds, setSelectedIds] = useState<readonly string[]>([])

  const selectedCandidates = useMemo(
    () =>
      selectedIds
        .map((id) => candidates.find((c) => c.agentId === id))
        .filter((c): c is PartyCandidateAgent => Boolean(c)),
    [selectedIds, candidates],
  )

  const selectedGuilds = useMemo(
    () => selectedCandidates.map((c) => c.guild),
    [selectedCandidates],
  )

  const synergyPreview = useMemo(
    () => pickStrongestSynergy(selectedGuilds, synergies),
    [selectedGuilds, synergies],
  )

  const trimmedName = name.trim()
  const selectionCount = selectedCandidates.length
  const sizeValid =
    selectionCount >= MIN_PARTY_SIZE && selectionCount <= MAX_PARTY_SIZE
  const nameValid = trimmedName.length > 0
  const canConfirm = !submitting && sizeValid && nameValid

  const toggleSelection = useCallback(
    (agentId: string) => {
      setSelectedIds((current) => {
        if (current.includes(agentId)) {
          return current.filter((id) => id !== agentId)
        }
        if (current.length >= MAX_PARTY_SIZE) return current
        return [...current, agentId]
      })
    },
    [],
  )

  const handleOpenChange = useCallback(
    (next: boolean) => {
      if (!next) onClose()
    },
    [onClose],
  )

  const handleConfirm = useCallback(() => {
    if (!canConfirm) return
    onConfirm({
      name: trimmedName,
      memberAgentIds: selectedCandidates.map((c) => c.agentId),
      memberGuilds: Object.fromEntries(
        selectedCandidates.map((c) => [c.agentId, c.guild]),
      ) as Record<string, AgentGuild>,
      synergy: synergyPreview,
    })
  }, [canConfirm, onConfirm, trimmedName, selectedCandidates, synergyPreview])

  if (!open) return null

  const atSelectionCap = selectionCount >= MAX_PARTY_SIZE
  const sizeHint = sizeValid
    ? `${selectionCount} of ${MAX_PARTY_SIZE} members selected`
    : selectionCount < MIN_PARTY_SIZE
      ? `Select at least ${MIN_PARTY_SIZE} agents (${selectionCount} so far)`
      : `Maximum ${MAX_PARTY_SIZE} agents per party`

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        className={cn(
          "max-h-[calc(100vh-2rem)] max-w-3xl overflow-y-auto",
          className,
        )}
        data-testid="party-builder-modal"
        data-selection-count={selectionCount}
        data-size-valid={sizeValid ? "true" : "false"}
        data-can-confirm={canConfirm ? "true" : "false"}
        data-synergy-label={synergyPreview?.label ?? ""}
      >
        <DialogHeader>
          <DialogTitle
            className="flex items-center gap-2 font-mono text-sm"
            data-testid="party-builder-modal-title"
          >
            <UserPlus className="size-4 text-violet-500" aria-hidden="true" />
            Form a new party
          </DialogTitle>
          <DialogDescription className="font-mono text-[11px] text-muted-foreground">
            Compose a {MIN_PARTY_SIZE}-{MAX_PARTY_SIZE} member party for a
            Tier&nbsp;L+ task. Cross-Guild composition unlocks a synergy
            bonus per ADR-0008 §W17.
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-4 lg:grid-cols-[minmax(0,1.05fr)_minmax(0,0.95fr)]">
          <section
            aria-label="Party composition"
            className="min-w-0 space-y-3"
            data-testid="party-builder-roster"
          >
            <div
              className="flex flex-col gap-1.5 text-xs"
              data-testid="party-builder-name-label"
            >
              <label
                htmlFor="party-builder-name"
                className="font-mono uppercase text-muted-foreground"
              >
                Party name
              </label>
              <Input
                id="party-builder-name"
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="e.g. Atlas, Borealis"
                aria-invalid={nameValid ? undefined : true}
                disabled={submitting}
                data-testid="party-builder-name-input"
                maxLength={80}
              />
            </div>

            <div
              className="flex items-center justify-between gap-2 rounded-md border bg-muted/30 px-2.5 py-1.5"
              data-testid="party-builder-size-hint"
              data-size-valid={sizeValid ? "true" : "false"}
            >
              <span className="inline-flex items-center gap-1.5 font-mono text-[11px]">
                <Users className="size-3.5" aria-hidden="true" />
                {sizeHint}
              </span>
              <span
                className={cn(
                  "rounded-full border px-1.5 py-0.5 font-mono text-[10px] uppercase",
                  sizeValid
                    ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
                    : "border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300",
                )}
              >
                {selectionCount}/{MAX_PARTY_SIZE}
              </span>
            </div>

            <div
              className="flex flex-col gap-1.5"
              data-testid="party-builder-candidates"
            >
              {candidates.length === 0 ? (
                <div
                  className="rounded-md border border-dashed bg-muted/20 p-4 text-center"
                  data-testid="party-builder-empty"
                >
                  <p className="text-xs font-medium">No eligible agents</p>
                  <p className="mt-1 text-[11px] text-muted-foreground">
                    Every agent is already in an active party.
                  </p>
                </div>
              ) : (
                candidates.map((candidate) => {
                  const selected = selectedIds.includes(candidate.agentId)
                  const capDisabled = !selected && atSelectionCap
                  return (
                    <CandidateRow
                      key={candidate.agentId}
                      candidate={candidate}
                      selected={selected}
                      disabled={capDisabled || Boolean(submitting)}
                      onToggle={() => toggleSelection(candidate.agentId)}
                    />
                  )
                })
              )}
            </div>
          </section>

          <section
            aria-label="Party preview"
            className="min-w-0 space-y-3"
            data-testid="party-builder-preview"
          >
            <div className="flex items-center gap-2 font-mono text-[10px] uppercase text-muted-foreground">
              <Sparkles className="size-3.5 text-violet-500" aria-hidden="true" />
              Synergy preview
            </div>

            <div data-testid="party-builder-synergy-preview">
              {selectionCount < MIN_PARTY_SIZE ? (
                <div
                  className="rounded-md border border-dashed bg-muted/15 p-4 text-center font-mono text-[11px] text-muted-foreground"
                  data-testid="party-builder-synergy-pending"
                >
                  Pick at least {MIN_PARTY_SIZE} agents to preview synergy.
                </div>
              ) : (
                <PartyBadge synergy={synergyPreview} />
              )}
            </div>

            <div
              className="rounded-md border bg-background/60 p-3"
              data-testid="party-builder-roster-summary"
            >
              <div className="mb-2 flex items-center justify-between gap-2">
                <span className="font-mono text-[10px] uppercase text-muted-foreground">
                  Selected agents
                </span>
                <Badge variant="outline" className="h-5 px-1.5 text-[10px]">
                  {selectionCount}
                </Badge>
              </div>
              {selectionCount === 0 ? (
                <p
                  className="font-mono text-[11px] text-muted-foreground"
                  data-testid="party-builder-roster-empty"
                >
                  No agents picked yet.
                </p>
              ) : (
                <ul className="flex flex-col gap-1.5">
                  {selectedCandidates.map((candidate) => (
                    <li
                      key={candidate.agentId}
                      className="flex items-center justify-between gap-2 font-mono text-[11px]"
                      data-testid="party-builder-roster-item"
                      data-agent-id={candidate.agentId}
                    >
                      <span className="truncate">{candidate.displayName}</span>
                      <span className="shrink-0 text-muted-foreground">
                        {candidate.guild}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            {errorMessage ? (
              <div
                className="rounded-md border border-rose-500/40 bg-rose-500/10 px-3 py-2 font-mono text-[11px] text-rose-700 dark:text-rose-300"
                data-testid="party-builder-error"
                role="alert"
              >
                {errorMessage}
              </div>
            ) : null}
          </section>
        </div>

        <DialogFooter className="gap-2">
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            className="inline-flex items-center justify-center rounded border border-border bg-card px-3 py-1.5 font-mono text-xs text-muted-foreground hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
            data-testid="party-builder-cancel"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleConfirm}
            disabled={!canConfirm}
            className="inline-flex items-center justify-center gap-1 rounded border border-violet-500/55 bg-violet-500/10 px-3 py-1.5 font-mono text-xs text-violet-700 hover:bg-violet-500/20 disabled:cursor-not-allowed disabled:opacity-50 dark:text-violet-300"
            data-testid="party-builder-confirm"
          >
            <UserPlus className="size-3" aria-hidden="true" />
            {submitting ? "Forming party…" : "Form party"}
          </button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export default PartyBuilderModal
