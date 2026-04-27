"use client"

/**
 * BS.9.3 — Bootstrap wizard vertical multi-pick body.
 *
 * Lives inside the ``vertical_setup`` step exposed by
 * ``app/bootstrap/page.tsx`` (BS.9.2). Five vertical chips
 * (D/W/P/S/X mapped to ``mobile`` / ``embedded`` / ``web`` / ``software``
 * / ``cross-toolchain`` per ``backend.bootstrap._verticals_chosen``) are
 * rendered as a checkbox grid; each checked vertical reveals an inline
 * sub-step hint pointing at the per-vertical configurator (Android API
 * range, BSP picker, runtime picker, toolchain picker, cross-compiler
 * picker — those land in BS.9.4).
 *
 * Pure presentational + callback-driven:
 *
 *   - props.initialSelected: pre-fill the picker (e.g., re-opening the
 *     step after a partial commit).
 *   - props.disabled: disables interaction (e.g., during an in-flight
 *     commit; BS.9.5 will set this while the batch enqueue resolves).
 *   - props.onCommit({verticals_selected}): operator clicked
 *     ``Confirm picks`` with ≥ 1 selection. BS.9.5 will turn this into
 *     a batch ``POST /installer/jobs`` and progress shows in the BS.7
 *     install drawer; for now ``app/bootstrap/page.tsx`` just flips
 *     ``localGreen.vertical_setup=true`` so the wizard advances.
 *
 * Skipping is **not** routed through this component — the parent
 * VerticalSetupStep wrapper still owns the Skip CTA (BS.9.2 contract).
 * That keeps the contract symmetric with ``GitForgeStep``: the wrapper
 * surfaces the "I'm done" affordance and the inner pick-form only
 * handles the positive path.
 *
 * Module-global state audit (per ``docs/sop/implement_phase_step.md``
 * Step 1): zero module-level mutable state. ``BOOTSTRAP_VERTICALS`` is
 * a frozen-at-import const tuple, ``BOOTSTRAP_VERTICAL_IDS`` /
 * ``BOOTSTRAP_VERTICAL_CODES`` derive from it. All component state is
 * React-local (``useState``); no cross-worker concern (this is
 * client-side only). Answer #1 — per-render stateless derivation.
 */

import { useCallback, useState } from "react"
import {
  Boxes,
  Check,
  ChevronRight,
  Cpu,
  FileCode,
  Globe,
  Layers,
  Smartphone,
} from "lucide-react"

/** Canonical vertical IDs, mirroring
 *  ``backend.bootstrap._verticals_chosen`` payload contract. The
 *  literal-union form keeps switch-cases exhaustive at compile time. */
export type BootstrapVerticalId =
  | "mobile"
  | "embedded"
  | "web"
  | "software"
  | "cross-toolchain"

/** D/W/P/S/X chip code letters per the BS ADR / backend docstring. */
export type BootstrapVerticalCode = "D" | "W" | "P" | "S" | "X"

export interface BootstrapVerticalDef {
  /** Stable canonical id — also the value written to
   *  ``bootstrap_state.metadata.verticals_selected``. */
  id: BootstrapVerticalId
  /** Single-letter chip code rendered on the card. Mnemonic per the
   *  ADR — operator-facing shortcode independent of the slug, so a
   *  future rename of ``cross-toolchain`` doesn't drag the chip
   *  visuals along. */
  code: BootstrapVerticalCode
  /** Long label shown next to the chip code on the card header. */
  label: string
  /** One-line operator hint describing what this vertical pulls in. */
  hint: string
  /** Lucide icon paired with the card. */
  icon: React.ComponentType<{ size?: number; className?: string }>
  /** Inline sub-step hint shown when the vertical is checked. Points
   *  at the BS.9.4 configurator that lands per-vertical. */
  subStepHint: string
}

/** Canonical vertical order. ``toggleVertical`` and the on-screen
 *  ``selected`` payload always serialize in this order so BS.9.5's
 *  batch enqueue is idempotent under user click order. */
export const BOOTSTRAP_VERTICALS: readonly BootstrapVerticalDef[] = [
  {
    id: "mobile",
    code: "D",
    label: "Mobile (Android / iOS)",
    hint: "Android SDK + iOS toolchain + emulator presets",
    icon: Smartphone,
    subStepHint:
      "Configure Android API range — landing in BS.9.4 (compile target / min API / emulator preset / GMS toggle).",
  },
  {
    id: "embedded",
    code: "W",
    label: "Embedded (RK / Allwinner / Aml)",
    hint: "RKDevTool / Allwinner LiveSuit / Amlogic USB Burning",
    icon: Cpu,
    subStepHint:
      "Pick BSP — landing in BS.9.4 (Rockchip / Allwinner / Amlogic / NXP variants + flash tools).",
  },
  {
    id: "web",
    code: "P",
    label: "Web",
    hint: "Node + version manager, Bun / Deno, browser engines",
    icon: Globe,
    subStepHint:
      "Pick runtime — landing in BS.9.4 (Node LTS / Bun / Deno toggle + browser-engine matrix).",
  },
  {
    id: "software",
    code: "S",
    label: "Software",
    hint: "Python + uv, Rust + rustup, Go, JVM",
    icon: FileCode,
    subStepHint:
      "Pick toolchains — landing in BS.9.4 (Python / Rust / Go / JVM combos + version pins).",
  },
  {
    id: "cross-toolchain",
    code: "X",
    label: "Cross-toolchain",
    hint: "Yocto BSP, MCUXpresso, Buildroot, vendor cross-compilers",
    icon: Boxes,
    subStepHint:
      "Pick cross-compiler — landing in BS.9.4 (Yocto / Buildroot / NXP MCUXpresso / vendor SDKs).",
  },
] as const

/** Canonical id list — derived from ``BOOTSTRAP_VERTICALS`` so the
 *  source of truth stays in one place. */
export const BOOTSTRAP_VERTICAL_IDS: readonly BootstrapVerticalId[] =
  BOOTSTRAP_VERTICALS.map((v) => v.id)

/** Canonical code list — useful for tests pinning the D/W/P/S/X
 *  contract independently of the slug rename. */
export const BOOTSTRAP_VERTICAL_CODES: readonly BootstrapVerticalCode[] =
  BOOTSTRAP_VERTICALS.map((v) => v.code)

/** Toggle a vertical id in/out of the selected set, returning a fresh
 *  array in canonical (BOOTSTRAP_VERTICAL_IDS) order. Pure — exported
 *  so BS.9.6 can pin the order invariant without DOM rendering. */
export function toggleVertical(
  selected: readonly BootstrapVerticalId[],
  id: BootstrapVerticalId,
): BootstrapVerticalId[] {
  const has = selected.includes(id)
  if (has) {
    return BOOTSTRAP_VERTICAL_IDS.filter(
      (vid) => selected.includes(vid) && vid !== id,
    )
  }
  return BOOTSTRAP_VERTICAL_IDS.filter(
    (vid) => selected.includes(vid) || vid === id,
  )
}

/** Coerce arbitrary data into a clean ``BootstrapVerticalId[]`` —
 *  drops unknown strings, preserves canonical order, dedupes. Used by
 *  the component's ``initialSelected`` prop and exported for unit
 *  tests / future re-open-step flows.
 *
 *  Forward-compat note: a future ``vertical: "rtos"`` (per BS ADR
 *  §3.4 catalog feed extensibility) lands here as an unknown id and
 *  collapses out — the ``vertical_setup`` step never silently
 *  enqueues an unknown vertical.
 */
export function coerceSelectedVerticals(value: unknown): BootstrapVerticalId[] {
  if (!Array.isArray(value)) return []
  const set = new Set<string>()
  for (const v of value) {
    if (typeof v === "string") set.add(v)
  }
  return BOOTSTRAP_VERTICAL_IDS.filter((vid) => set.has(vid))
}

/** Look up the chip code for a vertical id. Pure helper exported so
 *  copy-on-card stays in one place (vs. duplicating the find call). */
export function verticalCodeFor(id: BootstrapVerticalId): BootstrapVerticalCode {
  const def = BOOTSTRAP_VERTICALS.find((v) => v.id === id)
  // BootstrapVerticalId is a closed union over BOOTSTRAP_VERTICALS so
  // ``def`` is always defined; the fallback only exists to satisfy
  // the type checker without a non-null assertion.
  return def ? def.code : "D"
}

export interface BootstrapVerticalCommitPayload {
  /** Selected vertical IDs in canonical order — the value written to
   *  ``bootstrap_state.metadata.verticals_selected``. */
  verticals_selected: readonly BootstrapVerticalId[]
}

export interface BootstrapVerticalStepProps {
  /** Pre-fill the picker. Defaults to empty (fresh first-install
   *  flow). Re-opening the step after BS.9.5's commit will pass the
   *  prior payload here. */
  initialSelected?: readonly BootstrapVerticalId[]
  /** Disable interaction (e.g., while a parent commit is in flight).
   *  Defaults to false. */
  disabled?: boolean
  /** Fires when the operator clicks ``Confirm picks`` with ≥ 1
   *  selection. ``app/bootstrap/page.tsx`` translates this into
   *  ``localGreen.vertical_setup=true`` + cursor advance; BS.9.5 will
   *  add the batch ``/installer/jobs`` enqueue side-effect. */
  onCommit: (payload: BootstrapVerticalCommitPayload) => void
}

export default function BootstrapVerticalStep({
  initialSelected = [],
  disabled = false,
  onCommit,
}: BootstrapVerticalStepProps) {
  const [selected, setSelected] = useState<BootstrapVerticalId[]>(() =>
    coerceSelectedVerticals(initialSelected),
  )

  const onToggle = useCallback(
    (id: BootstrapVerticalId) => {
      if (disabled) return
      setSelected((prev) => toggleVertical(prev, id))
    },
    [disabled],
  )

  const onReset = useCallback(() => {
    if (disabled) return
    setSelected([])
  }, [disabled])

  const onConfirm = useCallback(() => {
    if (disabled) return
    if (selected.length === 0) return
    onCommit({ verticals_selected: [...selected] })
  }, [disabled, onCommit, selected])

  const canCommit = selected.length > 0 && !disabled
  const canReset = selected.length > 0 && !disabled
  const codes = selected.map((id) => verticalCodeFor(id)).join("")

  return (
    <div
      data-testid="bootstrap-vertical-pick"
      data-selected-count={selected.length}
      data-selected-codes={codes}
      data-disabled={disabled ? "true" : "false"}
      className="flex flex-col gap-3"
    >
      <div className="flex items-center gap-2 font-mono text-[10px] tracking-wider text-[var(--muted-foreground)]">
        <Layers size={12} />
        <span>VERTICAL MULTI-PICK</span>
        <span
          data-testid="bootstrap-vertical-pick-count"
          className="ml-auto"
        >
          {selected.length} / {BOOTSTRAP_VERTICALS.length} selected
        </span>
      </div>

      <ul
        data-testid="bootstrap-vertical-pick-grid"
        className="grid grid-cols-1 sm:grid-cols-2 gap-2 list-none p-0 m-0"
      >
        {BOOTSTRAP_VERTICALS.map((v) => {
          const checked = selected.includes(v.id)
          const Icon = v.icon
          return (
            <li key={v.id}>
              <button
                type="button"
                role="checkbox"
                aria-checked={checked}
                aria-label={`${v.label} (${v.code})`}
                data-testid={`bootstrap-vertical-pick-${v.id}`}
                data-code={v.code}
                data-checked={checked ? "true" : "false"}
                disabled={disabled}
                onClick={() => onToggle(v.id)}
                className={`w-full text-left flex flex-col gap-1.5 p-3 rounded border transition disabled:opacity-40 disabled:cursor-not-allowed ${
                  checked
                    ? "border-[var(--artifact-purple)] bg-[var(--artifact-purple)]/15"
                    : "border-[var(--border)] bg-[var(--background)] hover:border-[var(--foreground)]"
                }`}
              >
                <div className="flex items-center gap-2">
                  <span
                    data-testid={`bootstrap-vertical-pick-${v.id}-code`}
                    className={`inline-flex h-5 w-5 items-center justify-center rounded font-mono text-[10px] font-bold ${
                      checked
                        ? "bg-[var(--artifact-purple)] text-white"
                        : "bg-[var(--muted)]/40 text-[var(--muted-foreground)]"
                    }`}
                  >
                    {v.code}
                  </span>
                  <Icon
                    size={14}
                    className={
                      checked
                        ? "text-[var(--artifact-purple)]"
                        : "text-[var(--muted-foreground)]"
                    }
                  />
                  <span className="font-mono text-xs font-semibold">
                    {v.label}
                  </span>
                  {checked && (
                    <Check
                      size={12}
                      data-testid={`bootstrap-vertical-pick-${v.id}-tick`}
                      className="ml-auto text-[var(--artifact-purple)]"
                    />
                  )}
                </div>
                <p className="font-mono text-[10px] text-[var(--muted-foreground)] leading-relaxed">
                  {v.hint}
                </p>
                {checked && (
                  <div
                    data-testid={`bootstrap-vertical-substep-${v.id}`}
                    className="flex items-start gap-1.5 mt-1 px-2 py-1.5 rounded border border-dashed border-[var(--artifact-purple)]/50 bg-[var(--artifact-purple)]/5 font-mono text-[10px] text-[var(--foreground)] leading-relaxed"
                  >
                    <ChevronRight
                      size={11}
                      className="mt-0.5 shrink-0 text-[var(--artifact-purple)]"
                    />
                    <span>{v.subStepHint}</span>
                  </div>
                )}
              </button>
            </li>
          )
        })}
      </ul>

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          data-testid="bootstrap-vertical-pick-confirm"
          onClick={onConfirm}
          disabled={!canCommit}
          className="flex items-center gap-2 px-3 py-2 rounded bg-[var(--artifact-purple)] text-white font-mono text-xs font-semibold hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          <Check size={12} />
          Confirm picks ({selected.length})
        </button>
        <button
          type="button"
          data-testid="bootstrap-vertical-pick-reset"
          onClick={onReset}
          disabled={!canReset}
          className="px-3 py-2 rounded border border-[var(--border)] font-mono text-xs hover:bg-[var(--muted)]/40 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          Reset selections
        </button>
        {selected.length === 0 && (
          <span
            data-testid="bootstrap-vertical-pick-empty-hint"
            className="font-mono text-[10px] text-[var(--muted-foreground)]"
          >
            Pick at least one vertical, or use the Skip button below.
          </span>
        )}
      </div>
    </div>
  )
}
