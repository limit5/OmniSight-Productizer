/**
 * V0 #7 — Shared workspace chat panel.
 *
 * A single conversational-iteration chat surface used by all three
 * product workspaces (`web` / `mobile` / `software`). It drops into the
 * right column of `WorkspaceShell` and drives the "NL → task" loop:
 *
 *   1. The operator types a prompt (optionally pasting images or
 *      referencing an existing annotation).
 *   2. The chat submits the composed message to the caller via
 *      `onSubmitTask(message)`. The caller owns the actual backend
 *      plumbing — the chat panel is purely a composition surface.
 *   3. The agent's replies stream in as additional messages pushed by
 *      the caller into the `messages` prop, or appended via the
 *      imperative handle returned by `useWorkspaceChatController()`
 *      (future V0 #6 SSE wire-up will call `appendAgentMessage`).
 *
 * Why this component is workspace-agnostic by design:
 *   The three workspaces share the same iteration loop — "describe
 *   what you want, the agent tries, you refine." Forking per-type
 *   would quickly diverge on trivial wording; instead we stamp each
 *   outbound task with `workspaceType` (read from the enclosing
 *   `<WorkspaceProvider>` via `useWorkspaceType()`) and let the
 *   backend router dispatch by type. That keeps parity with V0 #6's
 *   SSE gate, which also keys on `workspace.type`.
 *
 * Scope for V0 #7:
 *   - Text composer with Enter-to-send / Shift+Enter-for-newline.
 *   - Image attachments (file input + drag-drop) rendered as thumbnail
 *     chips in the composer; submit bundles them into the task.
 *   - Optional annotation-reference chips (`annotations` prop) — for
 *     the web workspace these come from the preview click-to-annotate
 *     layer; mobile/software can inject their own shapes (e.g. a
 *     line-range reference from the code editor).
 *   - Message log with `role ∈ {user, agent, system}`, ISO timestamp
 *     and optional pending-state spinner.
 *   - `onSubmitTask` contract: a single structured payload
 *     `{text, images, annotationIds, workspaceType}`. The caller is
 *     responsible for translating that into an actual backend request.
 *
 * Explicitly OUT of scope (future checkboxes):
 *   - Live SSE streaming of agent replies         → V1/V2 per-track work
 *   - Rich annotation editor                      → V1 #317 Web track
 *   - File-upload persistence to backend storage  → V3 Software track
 *
 * This component reads `useWorkspaceType()` only when the `workspaceType`
 * prop is omitted — so host tests (command-center screens, storybook)
 * that render the chat outside a provider can pass the type explicitly.
 */
"use client"

import * as React from "react"
import { Paperclip, Send, X } from "lucide-react"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import {
  isWorkspaceType,
  type WorkspaceType,
} from "@/app/workspace/[type]/types"
import { useOptionalWorkspaceType } from "@/components/omnisight/workspace-context"
import { useDraftPersistence } from "@/hooks/use-draft-persistence"
import { useDraftRestore } from "@/hooks/use-draft-restore"

// ─── Public shapes ─────────────────────────────────────────────────────────

export type WorkspaceChatRole = "user" | "agent" | "system"

export interface WorkspaceChatAttachment {
  /** Stable id — caller-supplied, or auto-generated for file uploads. */
  id: string
  /** Display label (usually the file name). */
  name: string
  /** MIME type (defaults to `application/octet-stream` when unknown). */
  mimeType: string
  /** Byte length of the attachment; `null` when unknown. */
  sizeBytes: number | null
  /** Pre-resolved preview URL (object URL / remote thumbnail). */
  previewUrl: string | null
}

export interface WorkspaceChatAnnotation {
  /** Stable id referenced in outbound tasks. */
  id: string
  /** Human label shown on the chip (e.g. "Header.tsx L42-55"). */
  label: string
  /** Optional extended tooltip / screen-reader description. */
  description?: string
}

/**
 * W16.4 — inline preview embed carried inside a chat message.
 *
 * The backend's `preview.ready` SSE event (emitted by
 * `backend.web.web_preview_ready.emit_preview_ready`) carries the
 * sandbox URL once the W14 dev server reports ready; the SSE consumer
 * appends a system-role message with this `previewEmbed` set, and the
 * message renderer mounts an `<iframe>` plus a fullscreen toggle so
 * the operator never has to copy-paste the URL.
 */
export interface WorkspaceChatPreviewEmbed {
  /** Sandbox URL the iframe loads (host-port or W14.3 ingress). */
  url: string
  /** W14 workspace id — useful for the FE to dedupe across rebuilds. */
  workspaceId?: string
  /** Optional human-facing label rendered above the iframe. */
  label?: string
  /**
   * W16.5 — bump on each ``preview.hmr_reload`` SSE event.  The iframe
   * uses ``reloadCount`` as part of its React key so the FE consumer
   * can force a full re-mount when vite HMR is stuck (vite plugin
   * crash / out-of-band file edit / operator pressed "force reload").
   * Defaults to 0; absent reads as 0.  The iframe is *not* re-mounted
   * by HMR's normal path — vite's WebSocket patches the page in place
   * — so this counter is only consulted for the escape hatch.
   */
  reloadCount?: number
}

/**
 * W16.6 — vite-error trace carried inside a chat message.
 *
 * Mirrors the ``previewViteError`` field produced by
 * ``backend.web.preview_vite_error.build_chat_message_for_preview_vite_error``.
 * The backend emits two SSE event names — ``preview.vite_error`` (the
 * "我看到 X 有 error，正在修…" detection card) and
 * ``preview.vite_error_resolved`` (the "已修 ✓" badge card) — and the
 * SSE consumer appends a system-role message with ``previewViteError``
 * set so the chat surfaces the live trace.
 *
 * ``status`` discriminates which icon / colour bucket renders;
 * ``errorClass`` (one of the W15.6 self-fix classes:
 * ``syntax_error`` / ``undefined_symbol`` / ``import_path_typo`` /
 * ``unclassified``) is supplied on detection cards and omitted on
 * resolution cards (the operator already saw the class earlier).
 */
export type WorkspaceChatPreviewViteErrorStatus = "detected" | "resolved"

export interface WorkspaceChatPreviewViteError {
  /** W14 workspace id — scope key for the chat trace. */
  workspaceId: string
  /** Lifecycle status — drives the icon/colour bucket. */
  status: WorkspaceChatPreviewViteErrorStatus
  /** Pre-rendered chat-message body (e.g. "我看到 src/Header.tsx 有 syntax_error，正在修…"). */
  label: string
  /** Optional W15.6 class identifier (``syntax_error`` etc.). */
  errorClass?: string
  /** Optional human-friendly target identifier (typically a file path). */
  target?: string
  /** Optional W15.4 head-only signature for correlating detection ↔ resolution. */
  errorSignature?: string
  /** Optional repo-relative path of the file vite reported. */
  sourcePath?: string
  /** Optional 1-based line number inside ``sourcePath``. */
  sourceLine?: number
}

/**
 * W16.7 — next-step coaching menu carried inside a chat message.
 *
 * Mirrors the ``previewNextSteps`` field produced by
 * ``backend.web.preview_next_steps.build_chat_message_for_preview_next_steps``.
 * Once the W14 dev server reports ready, the backend fires
 * ``preview.next_steps`` carrying the four canonical follow-up
 * options (Vercel deploy / a11y scan / commit+PR / 繼續編輯). The SSE
 * consumer appends a system-role message with ``previewNextSteps``
 * set so the chat surface can render the coach card.
 *
 * The menu is purely advisory — clicking an option pre-fills the
 * composer with the matching slash command; the operator may also
 * dismiss the card and keep typing.  The agent never blocks on the
 * pick.
 */
export type WorkspaceChatPreviewNextStepKind =
  | "vercel_deploy"
  | "a11y_scan"
  | "commit_pr"
  | "continue_edit"

export interface WorkspaceChatPreviewNextStepOption {
  /** Stable identifier — drives icon / colour bucket on the FE. */
  kind: WorkspaceChatPreviewNextStepKind
  /** Bilingual human-facing display label. */
  label: string
  /** Pre-rendered slash command the FE pre-fills on click. */
  slashCommand: string
  /** When true the FE marks this row as the recommended choice (★). */
  recommended?: boolean
}

export interface WorkspaceChatPreviewNextSteps {
  /** W14 workspace id — scope key for the menu. */
  workspaceId: string
  /** Pre-rendered chat-message body (e.g. "Preview is live — what next?"). */
  label: string
  /** Row-spec-ordered four-option tuple. */
  options: WorkspaceChatPreviewNextStepOption[]
  /** Optional sandbox URL the menu deep-links to. */
  previewUrl?: string
}

export interface WorkspaceChatMessage {
  id: string
  role: WorkspaceChatRole
  text: string
  /** ISO-8601 timestamp of when the message was observed. */
  createdAt: string
  /** Pending = the message has been sent but not yet acknowledged. */
  pending?: boolean
  attachments?: WorkspaceChatAttachment[]
  annotationIds?: string[]
  /**
   * W16.4 — when set, the message renders an inline iframe loading the
   * sandbox URL, with a fullscreen-expand toggle. Mutually compatible
   * with `text`/`attachments` so a system message can carry both
   * "preview ready" prose and the iframe.
   */
  previewEmbed?: WorkspaceChatPreviewEmbed
  /**
   * W16.6 — when set, the message renders a vite-error trace card
   * ("我看到 X 有 error，正在修…" / "已修 ✓"). Distinct sibling field
   * to {@link previewEmbed} and {@link previewHmrReload} — the three
   * never co-render on a single message because the FE renderer
   * treats them as mount / refresh / error-trace respectively.
   */
  previewViteError?: WorkspaceChatPreviewViteError
  /**
   * W16.7 — when set, the message renders a "what next?" coach card
   * after the preview goes live (Vercel deploy / a11y scan /
   * commit+PR / 繼續編輯). Sibling field to the other preview-flavour
   * extensions; never co-renders with iframe / HMR / error-trace on
   * a single message.
   */
  previewNextSteps?: WorkspaceChatPreviewNextSteps
}

export interface WorkspaceChatSubmission {
  text: string
  attachments: WorkspaceChatAttachment[]
  annotationIds: string[]
  workspaceType: WorkspaceType
}

export interface WorkspaceChatProps {
  /** Optional override — otherwise the enclosing provider supplies it. */
  workspaceType?: WorkspaceType
  /** Full conversation log, newest last. Rendered verbatim. */
  messages?: WorkspaceChatMessage[]
  /** Annotation chips available to attach to the next message. */
  annotations?: WorkspaceChatAnnotation[]
  /**
   * Submission hook.  Receives the composed message and is expected
   * to return `void` (fire-and-forget) or a Promise the composer can
   * await before clearing.  The composer is cleared only when the
   * promise resolves; rejections leave the composer untouched so the
   * operator can retry.
   */
  onSubmitTask?: (submission: WorkspaceChatSubmission) => void | Promise<void>
  /** Disable the composer (e.g. agent busy, offline). */
  disabled?: boolean
  /** Override the composer placeholder (defaults to per-type hint). */
  placeholder?: string
  /** Override the header title (defaults to "Workspace chat"). */
  title?: string
  /**
   * Factory for attachment ids — pass a deterministic fn in tests so
   * the generated DOM is stable.  Defaults to `crypto.randomUUID()`
   * when available, otherwise a time-based fallback.
   */
  idFactory?: () => string
  /** Clock seam for deterministic timestamps in tests. */
  nowIso?: () => string
  /** Injected for test assertions around drag-and-drop. */
  readAttachmentsFromFiles?: (files: File[]) => WorkspaceChatAttachment[]
  /**
   * Q.6 (#300, checkbox 1) — when true, every keystroke in the
   * composer is persisted to ``PUT /user/drafts/{draftSlotKey}``
   * after a 500 ms debounce so an accidental refresh / device
   * switch does not lose the half-typed prompt. Defaults to true;
   * set false in tests that don't want to mock the network.
   */
  draftPersistenceEnabled?: boolean
  /** Override the slot key — defaults to ``chat:main`` per Q.6 spec. */
  draftSlotKey?: string
  className?: string
}

// ─── Defaults ──────────────────────────────────────────────────────────────

const PER_TYPE_PLACEHOLDER: Record<WorkspaceType, string> = {
  web: "Describe the UI change you want — paste a screenshot or reference a component annotation.",
  mobile: "Describe the flow or screen to build — drop a screenshot or reference a platform annotation.",
  software: "Describe the behaviour you want — paste a log snippet or reference a code annotation.",
}

const ROLE_LABEL: Record<WorkspaceChatRole, string> = {
  user: "You",
  agent: "Agent",
  system: "System",
}

export const WORKSPACE_CHAT_MAX_FILE_BYTES = 10 * 1024 * 1024 // 10 MB

/**
 * Stable id factory that still works in jsdom (which doesn't always
 * ship `crypto.randomUUID`).  Exported so tests can assert the fallback
 * branch when needed.
 */
export function defaultChatIdFactory(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID()
  }
  return `chat-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

export function defaultNowIso(): string {
  return new Date().toISOString()
}

/**
 * Convert File objects (from an `<input type="file">` change event or a
 * drag-drop) into chat attachments.  Pure so tests can drive it
 * directly without staging DataTransfer.
 */
export function filesToChatAttachments(
  files: File[],
  idFactory: () => string = defaultChatIdFactory,
): WorkspaceChatAttachment[] {
  return files
    .filter((f) => f.size <= WORKSPACE_CHAT_MAX_FILE_BYTES)
    .map((f) => {
      const previewUrl =
        typeof URL !== "undefined" &&
        typeof URL.createObjectURL === "function" &&
        f.type.startsWith("image/")
          ? URL.createObjectURL(f)
          : null
      return {
        id: idFactory(),
        name: f.name,
        mimeType: f.type || "application/octet-stream",
        sizeBytes: typeof f.size === "number" ? f.size : null,
        previewUrl,
      }
    })
}

/**
 * W16.4 — render an iframe + fullscreen toggle for a chat-message
 * preview embed. Pulled out as a sub-component so the fullscreen
 * state is local to the message (each preview message has its own
 * toggle independent of siblings).
 */
function ChatPreviewEmbed({
  messageId,
  embed,
}: {
  messageId: string
  embed: WorkspaceChatPreviewEmbed
}) {
  const [fullscreen, setFullscreen] = React.useState<boolean>(false)
  const titleId = `workspace-chat-preview-title-${messageId}`
  const containerCls = fullscreen
    ? "fixed inset-0 z-50 flex flex-col bg-background"
    : "mt-2 flex flex-col gap-1 rounded-md border border-border bg-background"
  const iframeCls = fullscreen
    ? "h-full w-full flex-1 border-0"
    : "h-64 w-full rounded-b-md border-0"
  // W16.5 — including ``reloadCount`` in the iframe key forces a full
  // re-mount whenever the upstream SSE consumer bumps the counter
  // (e.g. on a ``preview.hmr_reload`` event after a vite plugin
  // crash).  The normal HMR path patches the page in place via vite's
  // WebSocket and does NOT bump the key — the counter is only the
  // escape hatch.
  const reloadCount = embed.reloadCount ?? 0
  const iframeKey = `iframe-${messageId}-${reloadCount}`
  return (
    <div
      data-testid={`workspace-chat-message-preview-${messageId}`}
      data-fullscreen={fullscreen ? "true" : "false"}
      data-workspace-id={embed.workspaceId ?? ""}
      data-reload-count={String(reloadCount)}
      className={containerCls}
    >
      <div className="flex items-center justify-between gap-2 px-2 py-1 text-[11px] text-muted-foreground">
        <span
          id={titleId}
          data-testid={`workspace-chat-message-preview-label-${messageId}`}
          className="truncate"
        >
          {embed.label || "Live preview"}
        </span>
        <div className="flex items-center gap-1">
          <a
            data-testid={`workspace-chat-message-preview-open-${messageId}`}
            href={embed.url}
            target="_blank"
            rel="noopener noreferrer"
            className="rounded px-1.5 py-0.5 text-[11px] text-muted-foreground underline-offset-2 hover:underline"
          >
            Open in new tab
          </a>
          <button
            type="button"
            data-testid={`workspace-chat-message-preview-toggle-${messageId}`}
            aria-pressed={fullscreen}
            aria-label={
              fullscreen ? "Exit fullscreen preview" : "Expand preview to fullscreen"
            }
            onClick={() => setFullscreen((f) => !f)}
            className="rounded border border-border bg-background px-1.5 py-0.5 text-[11px] text-muted-foreground hover:bg-accent"
          >
            {fullscreen ? "Exit fullscreen" : "Fullscreen"}
          </button>
        </div>
      </div>
      <iframe
        key={iframeKey}
        data-testid={`workspace-chat-message-preview-iframe-${messageId}`}
        src={embed.url}
        title={embed.label || "Live preview"}
        aria-labelledby={titleId}
        sandbox="allow-scripts allow-same-origin allow-forms allow-popups"
        loading="lazy"
        className={iframeCls}
      />
    </div>
  )
}

/**
 * W16.6 — render a vite-error trace card for a chat message.
 *
 * Pulled out as a sub-component so the detection/resolution status
 * variants live in one place and the chat row stays readable. The
 * trace is *purely informational* — there is no operator action on
 * the card itself; the agent is already auto-fixing in the
 * background.  When the W15.6 self-fix succeeds the SSE consumer
 * appends a sibling resolution message ("已修 ✓"); the FE may then
 * choose to dim or fold the original detection card via the
 * ``data-status`` attribute.
 */
function ChatPreviewViteError({
  messageId,
  trace,
}: {
  messageId: string
  trace: WorkspaceChatPreviewViteError
}) {
  const isResolved = trace.status === "resolved"
  // Detection cards lean amber (in-flight); resolution cards lean
  // green (success).  Both stay subtle because the agent does the
  // heavy lifting — the chat is just narration.
  const containerCls = isResolved
    ? "mt-2 flex flex-col gap-1 rounded-md border border-emerald-500/40 bg-emerald-500/5 px-2 py-1.5 text-[12px]"
    : "mt-2 flex flex-col gap-1 rounded-md border border-amber-500/40 bg-amber-500/5 px-2 py-1.5 text-[12px]"
  return (
    <div
      data-testid={`workspace-chat-message-vite-error-${messageId}`}
      data-status={trace.status}
      data-workspace-id={trace.workspaceId}
      data-error-class={trace.errorClass ?? ""}
      data-error-signature={trace.errorSignature ?? ""}
      className={containerCls}
    >
      <div className="flex items-center justify-between gap-2 text-[11px] text-muted-foreground">
        <span data-testid={`workspace-chat-message-vite-error-status-${messageId}`}>
          {isResolved ? "Resolved" : "In flight"}
        </span>
        {trace.errorClass ? (
          <span
            data-testid={`workspace-chat-message-vite-error-class-${messageId}`}
            className="rounded-sm border border-border/60 bg-background/60 px-1 py-0.5 font-mono text-[10px]"
          >
            {trace.errorClass}
          </span>
        ) : null}
      </div>
      <div
        data-testid={`workspace-chat-message-vite-error-label-${messageId}`}
        className="whitespace-pre-wrap break-words text-foreground"
      >
        {trace.label}
      </div>
      {trace.sourcePath ? (
        <div
          data-testid={`workspace-chat-message-vite-error-source-${messageId}`}
          className="font-mono text-[10px] text-muted-foreground"
        >
          {trace.sourcePath}
          {typeof trace.sourceLine === "number" ? `:${trace.sourceLine}` : null}
        </div>
      ) : null}
    </div>
  )
}

/**
 * W16.7 — render the "what next?" coach menu for a chat message.
 *
 * Pulled out as a sub-component so the four-option layout lives in
 * one place and the chat row stays readable. The menu surfaces the
 * row-spec-ordered options (Vercel deploy / a11y scan / commit+PR /
 * 繼續編輯) and pre-fills the composer with the matching slash command
 * when the operator clicks an option.
 *
 * The card is purely advisory — clicking does *not* commit the
 * action; it just primes the composer so the operator can review,
 * edit, and submit. Dismissal is implicit (operator types something
 * else and the card scrolls away).
 *
 * The recommended option is marked with a ★ glyph so the operator's
 * eye lands on the most common pick first.
 */
function ChatPreviewNextSteps({
  messageId,
  steps,
  onPickOption,
}: {
  messageId: string
  steps: WorkspaceChatPreviewNextSteps
  onPickOption?: (slashCommand: string) => void
}) {
  return (
    <div
      data-testid={`workspace-chat-message-next-steps-${messageId}`}
      data-workspace-id={steps.workspaceId}
      className="mt-2 flex flex-col gap-1 rounded-md border border-sky-500/40 bg-sky-500/5 px-2 py-1.5 text-[12px]"
    >
      <div
        data-testid={`workspace-chat-message-next-steps-label-${messageId}`}
        className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground"
      >
        {steps.label}
      </div>
      <ul
        data-testid={`workspace-chat-message-next-steps-options-${messageId}`}
        className="flex flex-col gap-1"
      >
        {steps.options.map((opt) => (
          <li
            key={opt.kind}
            data-testid={`workspace-chat-message-next-steps-option-${messageId}-${opt.kind}`}
            data-kind={opt.kind}
            data-recommended={opt.recommended ? "true" : "false"}
            className="flex items-center justify-between gap-2"
          >
            <button
              type="button"
              data-testid={`workspace-chat-message-next-steps-button-${messageId}-${opt.kind}`}
              aria-label={`Pick ${opt.label}`}
              onClick={() => onPickOption?.(opt.slashCommand)}
              className={cn(
                "flex flex-1 items-center justify-between gap-2 rounded-sm border px-2 py-1 text-left text-foreground transition-colors hover:bg-accent/40",
                opt.recommended
                  ? "border-sky-500/60 bg-background/60"
                  : "border-border/60 bg-background/40",
              )}
            >
              <span className="truncate">
                {opt.recommended ? "★ " : null}
                {opt.label}
              </span>
              <span
                data-testid={`workspace-chat-message-next-steps-slash-${messageId}-${opt.kind}`}
                className="ml-2 truncate font-mono text-[10px] text-muted-foreground"
              >
                {opt.slashCommand}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}

/**
 * W16.6 — apply a ``preview.vite_error_resolved`` SSE event to a
 * message log so the matching in-flight detection card flips to
 * resolved without the SSE consumer having to know which message id
 * it lives at.
 *
 * Pure helper so the SSE consumer can call it from any layer without
 * pulling in React state.  Walks newest-last for the most recent
 * detection card whose ``previewViteError.workspaceId`` matches and
 * whose ``errorSignature`` correlates (when present) — flips that
 * card's ``status`` to ``"resolved"`` and swaps in the resolution
 * label.  Returns a NEW array; input is not mutated.
 *
 * When no matching detection card exists this is a no-op (returns
 * the input unchanged).  Match priority: same ``workspaceId`` and
 * same ``errorSignature`` first; fall back to same ``workspaceId``
 * with no signature filter when ``errorSignature`` is absent on the
 * incoming event.
 */
export function applyPreviewViteErrorResolvedToMessages(
  messages: WorkspaceChatMessage[],
  workspaceId: string,
  resolvedLabel: string,
  errorSignature?: string,
): WorkspaceChatMessage[] {
  if (!workspaceId) return messages
  // First pass — try to match on (workspaceId, errorSignature).
  let bumpedIdx = -1
  if (errorSignature) {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      const m = messages[i]
      const trace = m.previewViteError
      if (
        trace &&
        trace.workspaceId === workspaceId &&
        trace.status === "detected" &&
        trace.errorSignature === errorSignature
      ) {
        bumpedIdx = i
        break
      }
    }
  }
  // Fallback — match on workspaceId only.
  if (bumpedIdx < 0) {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      const m = messages[i]
      const trace = m.previewViteError
      if (
        trace &&
        trace.workspaceId === workspaceId &&
        trace.status === "detected"
      ) {
        bumpedIdx = i
        break
      }
    }
  }
  if (bumpedIdx < 0) return messages
  const next = messages.slice()
  const target = next[bumpedIdx]
  next[bumpedIdx] = {
    ...target,
    text: resolvedLabel,
    previewViteError: target.previewViteError
      ? {
          ...target.previewViteError,
          status: "resolved",
          label: resolvedLabel,
        }
      : target.previewViteError,
  }
  return next
}

/**
 * W16.5 — apply a ``preview.hmr_reload`` SSE event to a message log.
 *
 * Pure helper so the SSE consumer can call it from any layer without
 * pulling in React state.  Finds the existing message whose
 * ``previewEmbed.workspaceId`` matches *workspaceId* (the most recent
 * one wins — the W14 sandbox is per-workspace 1:1) and bumps its
 * ``reloadCount`` by one.  Optionally appends a fresh chat row when
 * the operator wants a textual confirmation ("Preview updated:
 * header bigger") in addition to the silent in-place HMR patch.
 *
 * Returns a NEW array with the bumped message; the input is not
 * mutated (immutable update so React re-renders).  When no matching
 * message exists, returns the input unchanged so a stray reload event
 * for a never-mounted preview is a no-op.
 */
export function applyPreviewHmrReloadToMessages(
  messages: WorkspaceChatMessage[],
  workspaceId: string,
): WorkspaceChatMessage[] {
  if (!workspaceId) return messages
  let mutated = false
  // Walk newest-last so the *most recent* iframe matching the
  // workspace id is the one that bumps — older history rows for the
  // same workspace stay frozen.  We iterate in reverse, flip a flag
  // on first match, then fall back to the original order.
  let bumpedIdx = -1
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const m = messages[i]
    if (
      m.previewEmbed &&
      m.previewEmbed.workspaceId === workspaceId &&
      m.previewEmbed.url
    ) {
      bumpedIdx = i
      break
    }
  }
  if (bumpedIdx < 0) return messages
  const next = messages.slice()
  const target = next[bumpedIdx]
  const prevCount = target.previewEmbed?.reloadCount ?? 0
  next[bumpedIdx] = {
    ...target,
    previewEmbed: target.previewEmbed
      ? { ...target.previewEmbed, reloadCount: prevCount + 1 }
      : target.previewEmbed,
  }
  mutated = true
  return mutated ? next : messages
}

function useResolvedWorkspaceType(
  override: WorkspaceType | undefined,
): WorkspaceType {
  // Read the provider unconditionally (hook rules) but via the
  // non-throwing variant — so the chat may also render in storybook
  // or command-center host surfaces with an explicit `workspaceType`.
  const fromProvider = useOptionalWorkspaceType()
  const resolved = override ?? fromProvider
  if (!resolved || !isWorkspaceType(resolved)) {
    throw new Error(
      `WorkspaceChat could not resolve a workspace type ` +
        `(override=${String(override)}, provider=${String(fromProvider)}). ` +
        `Pass a workspaceType prop or render inside <WorkspaceProvider>.`,
    )
  }
  return resolved
}

// ─── Component ─────────────────────────────────────────────────────────────

export function WorkspaceChat({
  workspaceType,
  messages,
  annotations,
  onSubmitTask,
  disabled = false,
  placeholder,
  title = "Workspace chat",
  idFactory = defaultChatIdFactory,
  nowIso = defaultNowIso,
  readAttachmentsFromFiles,
  draftPersistenceEnabled = true,
  draftSlotKey = "chat:main",
  className,
}: WorkspaceChatProps) {
  const resolvedType = useResolvedWorkspaceType(workspaceType)
  const log = React.useMemo<WorkspaceChatMessage[]>(
    () => (Array.isArray(messages) ? messages : []),
    [messages],
  )
  const availableAnnotations = React.useMemo<WorkspaceChatAnnotation[]>(
    () => (Array.isArray(annotations) ? annotations : []),
    [annotations],
  )

  const [draftText, setDraftText] = React.useState<string>("")
  // Q.6 (#300, checkbox 1) — debounced server-side draft persistence.
  // Watches ``draftText`` so an accidental tab close does not lose
  // the half-typed prompt.
  useDraftPersistence({
    slotKey: draftSlotKey,
    value: draftText,
    enabled: draftPersistenceEnabled,
  })
  // Q.6 (#300, checkbox 2) — restore the server-stored draft once on
  // mount. Only overwrites the composer if it is still empty at the
  // time the fetch resolves, so a fast typist who started before the
  // round-trip finished is not clobbered.
  useDraftRestore({
    slotKey: draftSlotKey,
    enabled: draftPersistenceEnabled,
    onRestore: (draft) => {
      setDraftText((prev) => (prev.length === 0 ? draft.content : prev))
    },
  })
  const [pendingAttachments, setPendingAttachments] = React.useState<WorkspaceChatAttachment[]>(
    [],
  )
  const [selectedAnnotationIds, setSelectedAnnotationIds] = React.useState<string[]>([])
  const [submitting, setSubmitting] = React.useState<boolean>(false)
  const [isDragging, setIsDragging] = React.useState<boolean>(false)
  const fileInputRef = React.useRef<HTMLInputElement | null>(null)
  const logEndRef = React.useRef<HTMLDivElement | null>(null)

  // Toggle annotations: click to select / click again to deselect.
  const toggleAnnotation = React.useCallback((id: string) => {
    setSelectedAnnotationIds((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id],
    )
  }, [])

  // Clear selections that reference annotations no longer present.
  React.useEffect(() => {
    const available = new Set(availableAnnotations.map((a) => a.id))
    setSelectedAnnotationIds((prev) => prev.filter((id) => available.has(id)))
  }, [availableAnnotations])

  const addAttachments = React.useCallback(
    (files: File[]) => {
      const fn = readAttachmentsFromFiles ?? ((xs: File[]) => filesToChatAttachments(xs, idFactory))
      const next = fn(files)
      if (next.length === 0) return
      setPendingAttachments((prev) => [...prev, ...next])
    },
    [idFactory, readAttachmentsFromFiles],
  )

  const removeAttachment = React.useCallback((id: string) => {
    setPendingAttachments((prev) => {
      const victim = prev.find((a) => a.id === id)
      if (victim?.previewUrl && typeof URL !== "undefined" && typeof URL.revokeObjectURL === "function") {
        try {
          URL.revokeObjectURL(victim.previewUrl)
        } catch {
          // revokeObjectURL fails silently on unsupported URLs (e.g. remote
          // thumbnails). We don't care — the object is going away anyway.
        }
      }
      return prev.filter((a) => a.id !== id)
    })
  }, [])

  const handleFileInput = React.useCallback(
    (event: React.ChangeEvent<HTMLInputElement>) => {
      const files = event.target.files ? Array.from(event.target.files) : []
      addAttachments(files)
      // Reset so selecting the same file twice still fires `onChange`.
      event.target.value = ""
    },
    [addAttachments],
  )

  const handleDrop = React.useCallback(
    (event: React.DragEvent<HTMLDivElement>) => {
      event.preventDefault()
      setIsDragging(false)
      if (!event.dataTransfer) return
      const files = Array.from(event.dataTransfer.files ?? [])
      addAttachments(files)
    },
    [addAttachments],
  )

  const canSubmit =
    !disabled &&
    !submitting &&
    (draftText.trim().length > 0 ||
      pendingAttachments.length > 0 ||
      selectedAnnotationIds.length > 0)

  const submit = React.useCallback(async () => {
    if (!canSubmit) return
    const submission: WorkspaceChatSubmission = {
      text: draftText.trim(),
      attachments: pendingAttachments,
      annotationIds: selectedAnnotationIds,
      workspaceType: resolvedType,
    }
    try {
      setSubmitting(true)
      const result = onSubmitTask?.(submission)
      if (result && typeof (result as Promise<void>).then === "function") {
        await result
      }
      // Success: clear the composer.
      setDraftText("")
      setPendingAttachments([])
      setSelectedAnnotationIds([])
    } catch {
      // Leave composer state intact so the operator can retry.
    } finally {
      setSubmitting(false)
    }
  }, [canSubmit, draftText, onSubmitTask, pendingAttachments, resolvedType, selectedAnnotationIds])

  const handleKeyDown = React.useCallback(
    (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault()
        void submit()
      }
    },
    [submit],
  )

  // Keep the log scrolled to the newest message whenever it grows.
  React.useEffect(() => {
    logEndRef.current?.scrollIntoView?.({ block: "end" })
  }, [log])

  const placeholderText = placeholder ?? PER_TYPE_PLACEHOLDER[resolvedType]

  return (
    <section
      data-testid="workspace-chat"
      data-workspace-type={resolvedType}
      data-submitting={submitting ? "true" : "false"}
      aria-label={`${title} — ${resolvedType}`}
      className={cn(
        "flex min-h-0 w-full flex-col overflow-hidden rounded-md border border-border bg-card/40",
        className,
      )}
    >
      <header className="flex h-9 shrink-0 items-center justify-between gap-2 border-b border-border px-3">
        <span className="truncate text-xs font-semibold uppercase tracking-wider text-muted-foreground">
          {title}
        </span>
        <span
          data-testid="workspace-chat-type"
          className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground"
        >
          {resolvedType}
        </span>
      </header>

      <ol
        data-testid="workspace-chat-log"
        aria-live="polite"
        aria-label="Conversation log"
        className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto px-3 py-2"
      >
        {log.length === 0 ? (
          <li
            data-testid="workspace-chat-empty"
            className="rounded-md border border-dashed border-border/70 bg-background/40 px-3 py-4 text-center text-xs text-muted-foreground"
          >
            No messages yet. Send a prompt to start iterating.
          </li>
        ) : (
          log.map((m) => (
            <li
              key={m.id}
              data-testid={`workspace-chat-message-${m.id}`}
              data-role={m.role}
              data-pending={m.pending ? "true" : "false"}
              className={cn(
                "flex flex-col gap-1 rounded-md px-3 py-2 text-sm",
                m.role === "user"
                  ? "bg-primary/10 text-foreground"
                  : m.role === "agent"
                    ? "bg-muted text-foreground"
                    : "bg-amber-500/10 text-foreground",
              )}
            >
              <div className="flex items-center justify-between gap-2 text-[10px] uppercase tracking-wider text-muted-foreground">
                <span data-testid={`workspace-chat-message-role-${m.id}`}>
                  {ROLE_LABEL[m.role]}
                </span>
                <span>
                  {m.pending ? "Sending…" : new Date(m.createdAt).toLocaleTimeString()}
                </span>
              </div>
              <div
                data-testid={`workspace-chat-message-text-${m.id}`}
                className="whitespace-pre-wrap break-words"
              >
                {m.text}
              </div>
              {m.attachments && m.attachments.length > 0 && (
                <ul
                  data-testid={`workspace-chat-message-attachments-${m.id}`}
                  className="flex flex-wrap gap-1 text-[11px] text-muted-foreground"
                >
                  {m.attachments.map((a) => (
                    <li
                      key={a.id}
                      data-testid={`workspace-chat-message-attachment-${m.id}-${a.id}`}
                      className="rounded-sm border border-border/60 bg-background/50 px-1.5 py-0.5"
                    >
                      {a.name}
                    </li>
                  ))}
                </ul>
              )}
              {m.annotationIds && m.annotationIds.length > 0 && (
                <ul
                  data-testid={`workspace-chat-message-annotations-${m.id}`}
                  className="flex flex-wrap gap-1 text-[11px] text-muted-foreground"
                >
                  {m.annotationIds.map((aid) => (
                    <li
                      key={aid}
                      data-testid={`workspace-chat-message-annotation-${m.id}-${aid}`}
                      className="rounded-sm bg-muted px-1.5 py-0.5"
                    >
                      @{aid}
                    </li>
                  ))}
                </ul>
              )}
              {m.previewEmbed && m.previewEmbed.url ? (
                <ChatPreviewEmbed
                  messageId={m.id}
                  embed={m.previewEmbed}
                />
              ) : null}
              {m.previewViteError && m.previewViteError.workspaceId ? (
                <ChatPreviewViteError
                  messageId={m.id}
                  trace={m.previewViteError}
                />
              ) : null}
              {m.previewNextSteps && m.previewNextSteps.workspaceId ? (
                <ChatPreviewNextSteps
                  messageId={m.id}
                  steps={m.previewNextSteps}
                  onPickOption={(slash) => setDraftText(slash)}
                />
              ) : null}
            </li>
          ))
        )}
        <div ref={logEndRef} data-testid="workspace-chat-log-end" />
      </ol>

      {availableAnnotations.length > 0 && (
        <div
          data-testid="workspace-chat-annotation-tray"
          className="flex flex-wrap gap-1 border-t border-border px-3 py-2"
        >
          {availableAnnotations.map((a) => {
            const active = selectedAnnotationIds.includes(a.id)
            return (
              <button
                key={a.id}
                type="button"
                data-testid={`workspace-chat-annotation-${a.id}`}
                data-active={active ? "true" : "false"}
                aria-pressed={active}
                title={a.description ?? a.label}
                onClick={() => toggleAnnotation(a.id)}
                disabled={disabled || submitting}
                className={cn(
                  "rounded-full border px-2 py-0.5 text-[11px] font-medium transition-colors",
                  active
                    ? "border-primary bg-primary/15 text-primary"
                    : "border-border bg-background text-muted-foreground hover:bg-accent",
                )}
              >
                @{a.label}
              </button>
            )
          })}
        </div>
      )}

      <div
        data-testid="workspace-chat-composer"
        data-dragging={isDragging ? "true" : "false"}
        onDragOver={(e) => {
          e.preventDefault()
          setIsDragging(true)
        }}
        onDragLeave={() => setIsDragging(false)}
        onDrop={handleDrop}
        className={cn(
          "flex flex-col gap-2 border-t border-border px-3 py-2",
          isDragging && "bg-accent/40",
        )}
      >
        {pendingAttachments.length > 0 && (
          <ul
            data-testid="workspace-chat-attachment-tray"
            className="flex flex-wrap gap-2"
          >
            {pendingAttachments.map((a) => (
              <li
                key={a.id}
                data-testid={`workspace-chat-attachment-${a.id}`}
                className="inline-flex items-center gap-1 rounded-md border border-border bg-background/80 px-2 py-1 text-[11px] text-muted-foreground"
              >
                {a.previewUrl ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={a.previewUrl}
                    alt={a.name}
                    data-testid={`workspace-chat-attachment-preview-${a.id}`}
                    className="h-4 w-4 rounded object-cover"
                  />
                ) : null}
                <span>{a.name}</span>
                <button
                  type="button"
                  data-testid={`workspace-chat-attachment-remove-${a.id}`}
                  aria-label={`Remove attachment ${a.name}`}
                  onClick={() => removeAttachment(a.id)}
                  disabled={disabled || submitting}
                  className="ml-1 inline-flex h-4 w-4 items-center justify-center rounded hover:bg-accent"
                >
                  <X className="size-3" aria-hidden="true" />
                </button>
              </li>
            ))}
          </ul>
        )}

        <div className="flex items-end gap-2">
          <Textarea
            data-testid="workspace-chat-input"
            aria-label="Chat message"
            placeholder={placeholderText}
            value={draftText}
            disabled={disabled || submitting}
            onChange={(e) => setDraftText(e.target.value)}
            onKeyDown={handleKeyDown}
            rows={2}
            className="min-h-[48px] flex-1 resize-none text-sm"
          />
          <div className="flex flex-col gap-1">
            <input
              ref={fileInputRef}
              type="file"
              multiple
              hidden
              data-testid="workspace-chat-file-input"
              onChange={handleFileInput}
            />
            <Button
              type="button"
              variant="outline"
              size="icon"
              data-testid="workspace-chat-attach-button"
              aria-label="Attach image"
              disabled={disabled || submitting}
              onClick={() => fileInputRef.current?.click()}
            >
              <Paperclip className="size-4" aria-hidden="true" />
            </Button>
            <Button
              type="button"
              size="icon"
              data-testid="workspace-chat-submit-button"
              aria-label="Send message"
              disabled={!canSubmit}
              onClick={() => void submit()}
            >
              <Send className="size-4" aria-hidden="true" />
            </Button>
          </div>
        </div>
      </div>
    </section>
  )
}

export default WorkspaceChat
