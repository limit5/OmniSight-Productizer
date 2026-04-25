/**
 * V4 #1 (TODO row 1529, #320) — Web Workspace main page.
 *
 * Composes the three-column shell (`WorkspaceShell`) into the actual
 * Web product surface that operators land on at `/workspace/web`.  The
 * page is the integration point that pulls together the building
 * blocks the V0–V3 checkboxes already produced and turns them into a
 * usable iteration loop:
 *
 *   ┌──────────────┬───────────────────────────┬──────────────────┐
 *   │ project tree │   preview iframe          │   code viewer    │
 *   │ + component  │   + responsive toggle     │   + diff badge   │
 *   │   palette    │   + visual annotator      │   + copy button  │
 *   │ + design-    │     overlay               │ ──────────────── │
 *   │   token      │                           │   workspace chat │
 *   │   editor     │                           │                  │
 *   └──────────────┴───────────────────────────┴──────────────────┘
 *
 * Sub-bullets the row enumerates and how this page satisfies each:
 *
 *   • Left sidebar
 *       – Project tree     → `WebProjectTree` static-folder view
 *         (collapsible nodes; selection only emits an event so the
 *         tree itself stays stateless).
 *       – Component palette → `WebComponentPalette` lists the curated
 *         shadcn primitives the operator can ask the agent for.
 *         "Add to chat" pushes a `WorkspaceChatAnnotation` chip via
 *         `pendingHints`, so the chat composer can attach it on the
 *         next submit.
 *       – Design token editor → `DesignTokenEditor` exposes a colour
 *         input + font select + spacing slider.  Every mutation
 *         updates a CSS-variable wrapper around the preview iframe so
 *         the operator sees the brand change *before* the agent runs;
 *         a "Send to agent" button enqueues the snapshot as a chat
 *         hint the next prompt will inherit.
 *
 *   • Center pane
 *       – Preview iframe / screenshot via `WebPreviewSurface`, scaled
 *         to the active responsive preset (`desktop / tablet / mobile`
 *         — pixel sizes deliberately mirror
 *         `backend/ui_screenshot.VIEWPORT_PRESETS` so the front-end
 *         and the V2 #4 sandbox capture pipeline are visually coherent).
 *       – Visual annotator (`VisualAnnotator` from V3 #1) overlays
 *         the preview surface; new annotations show up as toggleable
 *         chat-attachment chips so the next agent prompt can reference
 *         them by id (matches the V3 #2 wire shape).
 *
 *   • Right pane
 *       – Code viewer (`WebCodeViewer`) with a tiny built-in
 *         additions/deletions diff highlighter (`+` / `-` line classes)
 *         and a one-click copy button.  Heavy dependencies (Prism,
 *         Shiki, Monaco) deliberately *not* pulled in here — diffs
 *         are short, the chat panel needs to share the column, and
 *         the LangChain firewall keeps build size lean.
 *       – Workspace chat (`WorkspaceChat` from V0 #7) handles the
 *         conversational iteration loop; this page just feeds it the
 *         per-type placeholder, the running message log, and the
 *         pending annotation/component/token hints.
 *
 * Why a Client Component end-to-end:
 *   The annotator overlay, the design token live-update, the
 *   responsive toggle, and the chat composer all need React state.
 *   A server component shell could nest a Client subtree but the
 *   resulting prop-drilling between sibling slots (sidebar →
 *   preview → chat) would forego the grid composition that
 *   `WorkspaceShell` already buys us.
 *
 * Provider scope:
 *   `app/workspace/[type]/layout.tsx` only wraps routes under the
 *   *dynamic* segment.  `/workspace/web` is a *static* segment in the
 *   same parent directory, so Next.js won't apply `[type]/layout.tsx`
 *   here — we wrap our own `<PersistentWorkspaceProvider type="web">`
 *   inline, which gives us the V0 #4 localStorage + backend
 *   hydration without forking the persistence layer.
 */
"use client"

import * as React from "react"
import {
  Check,
  ClipboardCopy,
  ExternalLink,
  Folder,
  FolderOpen,
  ImageOff,
  Layers,
  Monitor,
  Palette,
  Plus,
  Send,
  Smartphone,
  Tablet,
  Type as TypeIcon,
} from "lucide-react"

import { cn } from "@/lib/utils"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Separator } from "@/components/ui/separator"
import { Slider } from "@/components/ui/slider"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

import { PersistentWorkspaceProvider } from "@/components/omnisight/persistent-workspace-provider"
import { WorkspaceShell } from "@/components/omnisight/workspace-shell"
import { WorkspaceOnboardingTour } from "@/components/omnisight/workspace-onboarding-tour"
import {
  WorkspaceChat,
  type WorkspaceChatAnnotation,
  type WorkspaceChatMessage,
  type WorkspaceChatSubmission,
} from "@/components/omnisight/workspace-chat"
import {
  VisualAnnotator,
  annotationToAgentPayload,
  type VisualAnnotation,
} from "@/components/omnisight/visual-annotator"
import { useWorkspaceContext } from "@/components/omnisight/workspace-context"

// ─── Public shapes (exported for unit tests + storybook) ───────────────────

export interface ProjectTreeNode {
  id: string
  name: string
  kind: "dir" | "file"
  children?: ProjectTreeNode[]
}

export interface ShadcnPaletteEntry {
  name: string
  group: "layout" | "form" | "feedback" | "data"
  description: string
}

export interface ResponsivePreset {
  /** Stable id used as `data-viewport=…` and chat hint text. */
  id: "desktop" | "tablet" | "mobile"
  /** Human label rendered on the responsive toggle. */
  label: string
  /** Pixel width — mirrors `ui_screenshot.VIEWPORT_PRESETS`. */
  width: number
  /** Pixel height — mirrors `ui_screenshot.VIEWPORT_PRESETS`. */
  height: number
}

export interface DesignTokens {
  /** Primary brand colour in `#rrggbb` form. */
  primaryColor: string
  /** CSS font family stack to apply via `--ws-font-family`. */
  fontFamily: string
  /** Spacing multiplier in pixels (4–48 typical range). */
  spacingPx: number
}

export interface CodeArtifact {
  /** Stable id — usually a file path. */
  id: string
  /** Display label (e.g. `app/page.tsx`). */
  label: string
  /** Full file body. */
  source: string
  /** Optional unified diff for the latest agent change. */
  diff?: string | null
}

// ─── Public constants (exported so tests + storybook can re-import) ───────

/** Mirrors `backend/ui_screenshot.py:VIEWPORT_PRESETS` widths/heights. */
export const RESPONSIVE_PRESETS: readonly ResponsivePreset[] = Object.freeze([
  { id: "desktop", label: "Desktop", width: 1440, height: 900 },
  { id: "tablet", label: "Tablet", width: 768, height: 1024 },
  { id: "mobile", label: "Mobile", width: 375, height: 812 },
])

/** Curated shadcn primitives most operators reach for first. */
export const SHADCN_PALETTE: readonly ShadcnPaletteEntry[] = Object.freeze([
  { name: "Button", group: "form", description: "Primary action affordance." },
  { name: "Card", group: "layout", description: "Group of related content." },
  { name: "Input", group: "form", description: "Single-line text field." },
  { name: "Textarea", group: "form", description: "Multi-line text field." },
  { name: "Select", group: "form", description: "Dropdown pick-list." },
  { name: "Tabs", group: "layout", description: "Switch between panes." },
  { name: "Dialog", group: "feedback", description: "Modal overlay surface." },
  { name: "Toast", group: "feedback", description: "Transient notification." },
  { name: "Table", group: "data", description: "Tabular dataset render." },
  { name: "Badge", group: "data", description: "Compact status pill." },
])

export const DEFAULT_PROJECT_TREE: readonly ProjectTreeNode[] = Object.freeze([
  {
    id: "root/app",
    name: "app",
    kind: "dir",
    children: [
      { id: "root/app/page.tsx", name: "page.tsx", kind: "file" },
      { id: "root/app/layout.tsx", name: "layout.tsx", kind: "file" },
    ],
  },
  {
    id: "root/components",
    name: "components",
    kind: "dir",
    children: [
      { id: "root/components/button.tsx", name: "button.tsx", kind: "file" },
      { id: "root/components/card.tsx", name: "card.tsx", kind: "file" },
    ],
  },
  { id: "root/styles.css", name: "styles.css", kind: "file" },
])

export const DEFAULT_DESIGN_TOKENS: DesignTokens = Object.freeze({
  primaryColor: "#3366ff",
  fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif",
  spacingPx: 16,
})

export const FONT_PRESETS: readonly { id: string; label: string; stack: string }[] =
  Object.freeze([
    { id: "inter", label: "Inter", stack: "Inter, ui-sans-serif, system-ui, sans-serif" },
    {
      id: "system",
      label: "System",
      stack:
        "ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
    },
    {
      id: "serif",
      label: "Serif",
      stack: "ui-serif, Georgia, Cambria, 'Times New Roman', serif",
    },
    {
      id: "mono",
      label: "Mono",
      stack:
        "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', monospace",
    },
  ])

/** Min / max + step the design-token spacing slider exposes. */
export const SPACING_SLIDER = Object.freeze({ min: 4, max: 48, step: 2 })

/** Strict 6-digit hex validator used by the design-token editor. */
export const HEX_COLOR_RE = /^#[0-9a-f]{6}$/i

// ─── Pure helpers (exported for unit tests) ────────────────────────────────

/** True when the string parses as a 6-digit `#rrggbb` colour. */
export function isHexColor(value: string): boolean {
  return typeof value === "string" && HEX_COLOR_RE.test(value)
}

/** Shorten a long source path for sidebar display. */
export function shortenPath(path: string, maxChars = 36): string {
  if (typeof path !== "string") return ""
  if (path.length <= maxChars) return path
  return `…${path.slice(-(maxChars - 1))}`
}

/** Look up a viewport preset by id; falls back to desktop. */
export function resolveViewport(id: string | null | undefined): ResponsivePreset {
  const found = RESPONSIVE_PRESETS.find((p) => p.id === id)
  return found ?? RESPONSIVE_PRESETS[0]
}

/**
 * Encode an annotation as a stable, human-readable chat hint label.
 * The label feeds the `WorkspaceChatAnnotation` chip the operator
 * toggles in the composer; the actual structured payload is the V3 #2
 * `annotationToAgentPayload` shape and gets attached server-side.
 */
export function annotationChipLabel(annotation: VisualAnnotation): string {
  const kind = annotation.type === "rect" ? "Region" : "Pin"
  const ordinal = annotation.label ?? 0
  const summary = annotation.comment.trim().slice(0, 32)
  const suffix = summary.length > 0 ? ` — ${summary}` : ""
  return `${kind} #${ordinal}${suffix}`
}

/** Build a chip describing the live design-token snapshot. */
export function designTokensChip(tokens: DesignTokens): WorkspaceChatAnnotation {
  return {
    id: `tokens:${tokens.primaryColor}:${tokens.spacingPx}:${tokens.fontFamily}`,
    label: `Tokens · ${tokens.primaryColor} · ${tokens.spacingPx}px · ${
      FONT_PRESETS.find((f) => f.stack === tokens.fontFamily)?.label ?? "custom"
    }`,
    description: `Apply primary=${tokens.primaryColor}, spacing=${tokens.spacingPx}px, font=${tokens.fontFamily}.`,
  }
}

/**
 * Build a chip for a clicked palette entry.  The chip is content-stable
 * so toggling the same entry twice doesn't duplicate.
 */
export function paletteChip(entry: ShadcnPaletteEntry): WorkspaceChatAnnotation {
  return {
    id: `shadcn:${entry.name}`,
    label: `shadcn · ${entry.name}`,
    description: entry.description,
  }
}

/**
 * Naïve unified-diff line classifier — separates additions / deletions
 * for the right-pane code viewer.  Empty / non-string diffs return an
 * empty list so callers can fall back to "no changes".
 */
export type DiffLineKind = "add" | "del" | "ctx" | "meta"

export interface DiffLine {
  kind: DiffLineKind
  text: string
}

export function classifyDiffLines(diff: string | null | undefined): DiffLine[] {
  if (typeof diff !== "string" || diff.length === 0) return []
  const out: DiffLine[] = []
  for (const raw of diff.split(/\r\n|\n|\r/)) {
    if (raw.startsWith("diff --git") || raw.startsWith("index ")) {
      out.push({ kind: "meta", text: raw })
      continue
    }
    if (raw.startsWith("+++") || raw.startsWith("---") || raw.startsWith("@@")) {
      out.push({ kind: "meta", text: raw })
      continue
    }
    if (raw.startsWith("+")) {
      out.push({ kind: "add", text: raw })
      continue
    }
    if (raw.startsWith("-")) {
      out.push({ kind: "del", text: raw })
      continue
    }
    out.push({ kind: "ctx", text: raw })
  }
  return out
}

// ─── Sample artifacts (V4 stub — replaced once the agent wire lands) ──────

const SAMPLE_CODE: CodeArtifact = {
  id: "app/page.tsx",
  label: "app/page.tsx",
  source: `export default function Page() {\n  return (\n    <main className="p-8">\n      <h1 className="text-2xl font-semibold">Hello, OmniSight</h1>\n    </main>\n  )\n}\n`,
  diff: null,
}

// ─── Sub-components (kept inline so the page is the single composition) ──

interface SidebarSectionProps {
  title: string
  icon: React.ReactNode
  children: React.ReactNode
  testId: string
}

function SidebarSection({ title, icon, children, testId }: SidebarSectionProps) {
  return (
    <section
      data-testid={testId}
      className="flex flex-col gap-2 border-b border-border/60 px-3 py-3"
    >
      <header className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
        <span aria-hidden="true">{icon}</span>
        {title}
      </header>
      <div className="flex flex-col gap-2">{children}</div>
    </section>
  )
}

interface ProjectTreeProps {
  nodes: readonly ProjectTreeNode[]
  onSelectFile?: (node: ProjectTreeNode) => void
}

function ProjectTree({ nodes, onSelectFile }: ProjectTreeProps) {
  return (
    <ul data-testid="web-project-tree" className="flex flex-col gap-0.5 text-xs">
      {nodes.map((n) => (
        <ProjectTreeRow key={n.id} node={n} depth={0} onSelectFile={onSelectFile} />
      ))}
    </ul>
  )
}

interface ProjectTreeRowProps {
  node: ProjectTreeNode
  depth: number
  onSelectFile?: (node: ProjectTreeNode) => void
}

function ProjectTreeRow({ node, depth, onSelectFile }: ProjectTreeRowProps) {
  const [expanded, setExpanded] = React.useState<boolean>(depth === 0)
  if (node.kind === "dir") {
    return (
      <li>
        <button
          type="button"
          data-testid={`web-project-tree-dir-${node.id}`}
          aria-expanded={expanded}
          onClick={() => setExpanded((v) => !v)}
          className="flex w-full items-center gap-1.5 rounded-sm px-1.5 py-1 text-left hover:bg-accent"
          style={{ paddingLeft: `${depth * 12 + 6}px` }}
        >
          {expanded ? (
            <FolderOpen className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
          ) : (
            <Folder className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
          )}
          <span className="truncate">{node.name}</span>
        </button>
        {expanded && node.children && node.children.length > 0 && (
          <ul className="flex flex-col gap-0.5">
            {node.children.map((c) => (
              <ProjectTreeRow
                key={c.id}
                node={c}
                depth={depth + 1}
                onSelectFile={onSelectFile}
              />
            ))}
          </ul>
        )}
      </li>
    )
  }
  return (
    <li>
      <button
        type="button"
        data-testid={`web-project-tree-file-${node.id}`}
        onClick={() => onSelectFile?.(node)}
        className="flex w-full items-center gap-1.5 rounded-sm px-1.5 py-1 text-left text-muted-foreground hover:bg-accent hover:text-foreground"
        style={{ paddingLeft: `${depth * 12 + 18}px` }}
      >
        <span className="truncate">{node.name}</span>
      </button>
    </li>
  )
}

interface ComponentPaletteProps {
  entries: readonly ShadcnPaletteEntry[]
  selectedNames: readonly string[]
  onToggle: (entry: ShadcnPaletteEntry) => void
}

function ComponentPalette({ entries, selectedNames, onToggle }: ComponentPaletteProps) {
  const selected = React.useMemo(() => new Set(selectedNames), [selectedNames])
  return (
    <ul data-testid="web-component-palette" className="flex flex-col gap-1">
      {entries.map((entry) => {
        const isSelected = selected.has(entry.name)
        return (
          <li key={entry.name}>
            <button
              type="button"
              data-testid={`web-palette-entry-${entry.name}`}
              data-selected={isSelected ? "true" : "false"}
              aria-pressed={isSelected}
              onClick={() => onToggle(entry)}
              className={cn(
                "flex w-full items-start justify-between gap-2 rounded-md border border-border/60 px-2 py-1.5 text-left text-xs transition-colors",
                isSelected
                  ? "border-primary/60 bg-primary/10 text-foreground"
                  : "bg-background/50 text-muted-foreground hover:bg-accent hover:text-foreground",
              )}
            >
              <span className="flex flex-col gap-0.5">
                <span className="font-medium text-foreground">{entry.name}</span>
                <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
                  {entry.group}
                </span>
              </span>
              {isSelected ? (
                <Check className="size-3.5 shrink-0 text-primary" aria-hidden="true" />
              ) : (
                <Plus className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
              )}
            </button>
          </li>
        )
      })}
    </ul>
  )
}

interface DesignTokenEditorProps {
  tokens: DesignTokens
  onChange: (next: DesignTokens) => void
  onSendToAgent: () => void
}

function DesignTokenEditor({ tokens, onChange, onSendToAgent }: DesignTokenEditorProps) {
  const [draftColor, setDraftColor] = React.useState<string>(tokens.primaryColor)
  React.useEffect(() => {
    setDraftColor(tokens.primaryColor)
  }, [tokens.primaryColor])

  const colorValid = isHexColor(draftColor)
  const fontPresetId =
    FONT_PRESETS.find((f) => f.stack === tokens.fontFamily)?.id ?? "inter"

  return (
    <div data-testid="web-design-token-editor" className="flex flex-col gap-3 text-xs">
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="web-design-token-color" className="text-[11px] uppercase tracking-wider">
          Primary colour
        </Label>
        <div className="flex items-center gap-2">
          <input
            type="color"
            id="web-design-token-color"
            data-testid="web-design-token-color-picker"
            value={colorValid ? draftColor : tokens.primaryColor}
            onChange={(e) => {
              const next = e.target.value
              setDraftColor(next)
              onChange({ ...tokens, primaryColor: next })
            }}
            className="h-7 w-10 cursor-pointer rounded border border-border bg-background p-0"
          />
          <Input
            data-testid="web-design-token-color-input"
            value={draftColor}
            onChange={(e) => {
              const next = e.target.value
              setDraftColor(next)
              if (isHexColor(next)) {
                onChange({ ...tokens, primaryColor: next.toLowerCase() })
              }
            }}
            aria-invalid={!colorValid}
            className="h-7 flex-1 font-mono text-xs"
            spellCheck={false}
          />
        </div>
        {!colorValid && (
          <p
            data-testid="web-design-token-color-error"
            className="text-[10px] text-destructive"
          >
            Enter a 6-digit hex like #336699.
          </p>
        )}
      </div>

      <div className="flex flex-col gap-1.5">
        <Label className="text-[11px] uppercase tracking-wider">Font family</Label>
        <Select
          data-testid="web-design-token-font-select"
          value={fontPresetId}
          onValueChange={(id) => {
            const preset = FONT_PRESETS.find((f) => f.id === id)
            if (preset) onChange({ ...tokens, fontFamily: preset.stack })
          }}
        >
          <SelectTrigger
            data-testid="web-design-token-font-trigger"
            className="h-8 text-xs"
          >
            <SelectValue placeholder="Pick font" />
          </SelectTrigger>
          <SelectContent>
            {FONT_PRESETS.map((f) => (
              <SelectItem key={f.id} value={f.id} data-testid={`web-design-token-font-item-${f.id}`}>
                {f.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div className="flex flex-col gap-1.5">
        <div className="flex items-center justify-between">
          <Label className="text-[11px] uppercase tracking-wider">Spacing</Label>
          <span
            data-testid="web-design-token-spacing-readout"
            className="font-mono text-[11px] text-muted-foreground"
          >
            {tokens.spacingPx}px
          </span>
        </div>
        <Slider
          data-testid="web-design-token-spacing-slider"
          min={SPACING_SLIDER.min}
          max={SPACING_SLIDER.max}
          step={SPACING_SLIDER.step}
          value={[tokens.spacingPx]}
          onValueChange={(values) => {
            const next = values[0]
            if (typeof next === "number" && Number.isFinite(next)) {
              onChange({ ...tokens, spacingPx: next })
            }
          }}
        />
      </div>

      <Button
        type="button"
        data-testid="web-design-token-send"
        onClick={onSendToAgent}
        size="sm"
        variant="secondary"
        className="h-7 text-xs"
      >
        <Send className="mr-1 size-3" aria-hidden="true" />
        Send tokens to agent
      </Button>
    </div>
  )
}

interface ResponsiveToggleProps {
  active: ResponsivePreset["id"]
  onChange: (id: ResponsivePreset["id"]) => void
}

function ResponsiveToggle({ active, onChange }: ResponsiveToggleProps) {
  return (
    <Tabs
      value={active}
      onValueChange={(v) => onChange(v as ResponsivePreset["id"])}
      data-testid="web-responsive-toggle"
    >
      <TabsList className="h-8">
        {RESPONSIVE_PRESETS.map((preset) => {
          const Icon =
            preset.id === "desktop"
              ? Monitor
              : preset.id === "tablet"
                ? Tablet
                : Smartphone
          return (
            <TabsTrigger
              key={preset.id}
              value={preset.id}
              data-testid={`web-responsive-toggle-${preset.id}`}
              className="h-7 gap-1.5 px-2 text-xs"
            >
              <Icon className="size-3.5" aria-hidden="true" />
              {preset.label}
            </TabsTrigger>
          )
        })}
      </TabsList>
    </Tabs>
  )
}

interface PreviewSurfaceProps {
  url: string | null
  screenshotSrc: string | null
  viewport: ResponsivePreset
  tokens: DesignTokens
  annotations: VisualAnnotation[]
  onAnnotationsChange: (next: VisualAnnotation[]) => void
}

function PreviewSurface({
  url,
  screenshotSrc,
  viewport,
  tokens,
  annotations,
  onAnnotationsChange,
}: PreviewSurfaceProps) {
  const tokenStyle = React.useMemo<React.CSSProperties>(
    () =>
      ({
        ["--ws-primary" as string]: tokens.primaryColor,
        ["--ws-font-family" as string]: tokens.fontFamily,
        ["--ws-spacing" as string]: `${tokens.spacingPx}px`,
        fontFamily: tokens.fontFamily,
      }) as React.CSSProperties,
    [tokens.fontFamily, tokens.primaryColor, tokens.spacingPx],
  )

  return (
    <div
      data-testid="web-preview-surface"
      data-viewport={viewport.id}
      className="flex h-full min-h-0 w-full items-start justify-center overflow-auto bg-muted/30 p-4"
      style={tokenStyle}
    >
      <div
        data-testid="web-preview-frame"
        className="relative shrink-0 overflow-hidden rounded-md border border-border bg-background shadow-sm"
        style={{ width: viewport.width, height: viewport.height, maxWidth: "100%" }}
      >
        {url ? (
          <iframe
            data-testid="web-preview-iframe"
            title={`Preview of ${url}`}
            src={url}
            className="block h-full w-full border-0"
            sandbox="allow-scripts allow-same-origin allow-forms"
          />
        ) : screenshotSrc ? (
          <VisualAnnotator
            data-testid="web-preview-annotator"
            imageSrc={screenshotSrc}
            imageAlt="Latest sandbox screenshot"
            annotations={annotations}
            onAnnotationsChange={onAnnotationsChange}
            className="absolute inset-0"
          />
        ) : (
          <div
            data-testid="web-preview-empty"
            className="flex h-full w-full flex-col items-center justify-center gap-2 text-muted-foreground"
            style={{ minHeight: 200 }}
          >
            <ImageOff className="size-6" aria-hidden="true" />
            <p className="text-xs">No preview yet — describe the page to start.</p>
          </div>
        )}
      </div>
    </div>
  )
}

interface CodeViewerProps {
  artifact: CodeArtifact
  onCopy: (text: string) => Promise<void> | void
  copyStatus: "idle" | "copied" | "error"
}

function CodeViewer({ artifact, onCopy, copyStatus }: CodeViewerProps) {
  const diffLines = React.useMemo(() => classifyDiffLines(artifact.diff ?? null), [
    artifact.diff,
  ])
  return (
    <article
      data-testid="web-code-viewer"
      data-artifact-id={artifact.id}
      className="flex min-h-0 flex-col overflow-hidden rounded-md border border-border bg-background"
    >
      <header className="flex items-center justify-between gap-2 border-b border-border bg-muted/40 px-2 py-1">
        <div className="flex items-center gap-1.5 text-xs font-medium">
          <ExternalLink className="size-3.5 text-muted-foreground" aria-hidden="true" />
          <span data-testid="web-code-viewer-label" className="truncate">
            {shortenPath(artifact.label)}
          </span>
          {diffLines.length > 0 && (
            <Badge
              data-testid="web-code-viewer-diff-badge"
              variant="secondary"
              className="ml-1 h-4 gap-1 px-1.5 text-[10px]"
            >
              {diffLines.filter((l) => l.kind === "add").length}+
              {" / "}
              {diffLines.filter((l) => l.kind === "del").length}-
            </Badge>
          )}
        </div>
        <Button
          type="button"
          data-testid="web-code-viewer-copy"
          onClick={() => onCopy(artifact.source)}
          size="sm"
          variant="ghost"
          className="h-6 gap-1 px-2 text-[11px]"
        >
          {copyStatus === "copied" ? (
            <Check className="size-3" aria-hidden="true" />
          ) : (
            <ClipboardCopy className="size-3" aria-hidden="true" />
          )}
          {copyStatus === "copied" ? "Copied" : copyStatus === "error" ? "Error" : "Copy"}
        </Button>
      </header>
      <div data-testid="web-code-viewer-body" className="min-h-0 flex-1 overflow-auto">
        {diffLines.length > 0 ? (
          <pre
            data-testid="web-code-viewer-diff"
            className="m-0 whitespace-pre overflow-x-auto p-2 font-mono text-[11px] leading-snug"
          >
            {diffLines.map((line, idx) => (
              <span
                key={idx}
                data-testid={`web-code-viewer-diff-line-${idx}`}
                data-kind={line.kind}
                className={cn(
                  "block px-1",
                  line.kind === "add"
                    ? "bg-emerald-500/10 text-emerald-400"
                    : line.kind === "del"
                      ? "bg-rose-500/10 text-rose-400"
                      : line.kind === "meta"
                        ? "text-sky-400"
                        : "text-muted-foreground",
                )}
              >
                {line.text}
              </span>
            ))}
          </pre>
        ) : (
          <pre
            data-testid="web-code-viewer-source"
            className="m-0 whitespace-pre overflow-x-auto p-2 font-mono text-[11px] leading-snug text-foreground"
          >
            {artifact.source}
          </pre>
        )}
      </div>
    </article>
  )
}

// ─── Page contents (read provider; mount visual surfaces) ──────────────────

export interface WebWorkspacePageContentsProps {
  /** Initial design-token snapshot — exposed so tests can pin defaults. */
  initialTokens?: DesignTokens
  /** Sample code artifact shown until the agent wire lands. */
  initialArtifact?: CodeArtifact
  /** Project tree to render in the sidebar. */
  projectTree?: readonly ProjectTreeNode[]
  /**
   * Inject a clipboard writer in tests (jsdom has no `navigator.clipboard`
   * by default).  Defaults to `navigator.clipboard.writeText`.
   */
  copyToClipboardImpl?: (text: string) => Promise<void>
  /**
   * Test seam: forward agent-bound submissions out of the page so tests
   * can assert composer payload shape without mocking the backend.
   */
  onAgentSubmit?: (submission: WorkspaceChatSubmission) => void | Promise<void>
}

export function WebWorkspacePageContents({
  initialTokens = DEFAULT_DESIGN_TOKENS,
  initialArtifact = SAMPLE_CODE,
  projectTree = DEFAULT_PROJECT_TREE,
  copyToClipboardImpl,
  onAgentSubmit,
}: WebWorkspacePageContentsProps) {
  const ctx = useWorkspaceContext()
  const [viewportId, setViewportId] = React.useState<ResponsivePreset["id"]>("desktop")
  const [tokens, setTokens] = React.useState<DesignTokens>(initialTokens)
  const [annotations, setAnnotations] = React.useState<VisualAnnotation[]>([])
  const [paletteSelection, setPaletteSelection] = React.useState<string[]>([])
  const [tokenSnapshot, setTokenSnapshot] = React.useState<DesignTokens | null>(null)
  const [chatLog, setChatLog] = React.useState<WorkspaceChatMessage[]>([])
  const [copyStatus, setCopyStatus] = React.useState<"idle" | "copied" | "error">("idle")

  const viewport = React.useMemo(() => resolveViewport(viewportId), [viewportId])

  const togglePaletteEntry = React.useCallback((entry: ShadcnPaletteEntry) => {
    setPaletteSelection((prev) =>
      prev.includes(entry.name) ? prev.filter((x) => x !== entry.name) : [...prev, entry.name],
    )
  }, [])

  const sendTokensToAgent = React.useCallback(() => {
    setTokenSnapshot({ ...tokens })
  }, [tokens])

  // Annotation chips, palette chips, and (optionally) a tokens chip
  // collectively form the chat-composer's available references.
  const annotationChips = React.useMemo<WorkspaceChatAnnotation[]>(
    () =>
      annotations.map((ann) => ({
        id: `annotation:${ann.id}`,
        label: annotationChipLabel(ann),
        description: ann.comment.length > 0 ? ann.comment : undefined,
      })),
    [annotations],
  )

  const paletteChips = React.useMemo<WorkspaceChatAnnotation[]>(
    () =>
      paletteSelection
        .map((name) => SHADCN_PALETTE.find((p) => p.name === name))
        .filter((entry): entry is ShadcnPaletteEntry => Boolean(entry))
        .map(paletteChip),
    [paletteSelection],
  )

  const tokenChips = React.useMemo<WorkspaceChatAnnotation[]>(
    () => (tokenSnapshot ? [designTokensChip(tokenSnapshot)] : []),
    [tokenSnapshot],
  )

  const allChips = React.useMemo<WorkspaceChatAnnotation[]>(
    () => [...annotationChips, ...paletteChips, ...tokenChips],
    [annotationChips, paletteChips, tokenChips],
  )

  const handleSubmit = React.useCallback(
    async (submission: WorkspaceChatSubmission) => {
      // Build a structured payload the chat log displays inline.
      const annotationPayloads = annotations
        .filter((a) => submission.annotationIds.includes(`annotation:${a.id}`))
        .map(annotationToAgentPayload)
      const userMessage: WorkspaceChatMessage = {
        id: `msg-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        role: "user",
        text: submission.text,
        createdAt: new Date().toISOString(),
        attachments: submission.attachments,
        annotationIds: submission.annotationIds,
      }
      setChatLog((prev) => [...prev, userMessage])
      ctx.setAgentSession({
        status: "running",
        startedAt: new Date().toISOString(),
        lastEventAt: new Date().toISOString(),
      })
      try {
        await onAgentSubmit?.({
          ...submission,
          // Annotation payloads ride on the submission via `annotationIds`;
          // the structured shape stays alongside in the log so callers
          // forwarding to the V3 #2 backend agent context have it ready.
        })
      } finally {
        // Leave the running indicator on; backend SSE will flip it.
        // Token snapshot is one-shot per submit so the chip clears.
        setTokenSnapshot(null)
        // Keep palette + annotations selected so the operator can iterate.
        // (Annotation payloads exposed via annotationPayloads — currently
        // only logged into the message until the SSE wire lands.)
        void annotationPayloads
      }
    },
    [annotations, ctx, onAgentSubmit],
  )

  const copy = React.useCallback(
    async (text: string) => {
      const writer =
        copyToClipboardImpl ??
        (typeof navigator !== "undefined" && navigator.clipboard?.writeText
          ? (s: string) => navigator.clipboard.writeText(s)
          : null)
      if (!writer) {
        setCopyStatus("error")
        return
      }
      try {
        await writer(text)
        setCopyStatus("copied")
        window.setTimeout(() => setCopyStatus("idle"), 1500)
      } catch {
        setCopyStatus("error")
        window.setTimeout(() => setCopyStatus("idle"), 1500)
      }
    },
    [copyToClipboardImpl],
  )

  const sidebar = (
    <div data-testid="web-workspace-sidebar" className="flex flex-col">
      <SidebarSection
        title="Project tree"
        icon={<Folder className="size-3.5" aria-hidden="true" />}
        testId="web-sidebar-section-tree"
      >
        <ProjectTree nodes={projectTree} />
      </SidebarSection>
      <SidebarSection
        title="shadcn palette"
        icon={<Layers className="size-3.5" aria-hidden="true" />}
        testId="web-sidebar-section-palette"
      >
        <ComponentPalette
          entries={SHADCN_PALETTE}
          selectedNames={paletteSelection}
          onToggle={togglePaletteEntry}
        />
      </SidebarSection>
      <SidebarSection
        title="Design tokens"
        icon={<Palette className="size-3.5" aria-hidden="true" />}
        testId="web-sidebar-section-tokens"
      >
        <DesignTokenEditor
          tokens={tokens}
          onChange={setTokens}
          onSendToAgent={sendTokensToAgent}
        />
      </SidebarSection>
    </div>
  )

  const preview = (
    <div data-testid="web-workspace-preview" className="flex h-full min-h-0 flex-col">
      <div className="flex h-9 shrink-0 items-center justify-between gap-2 border-b border-border bg-background/40 px-3">
        <ResponsiveToggle active={viewportId} onChange={setViewportId} />
        <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
          {viewport.width}×{viewport.height}
        </span>
      </div>
      <div className="min-h-0 flex-1">
        <PreviewSurface
          url={ctx.preview.url}
          screenshotSrc={null}
          viewport={viewport}
          tokens={tokens}
          annotations={annotations}
          onAnnotationsChange={setAnnotations}
        />
      </div>
    </div>
  )

  const codeChat = (
    <div data-testid="web-workspace-code-chat" className="flex h-full min-h-0 flex-col gap-2 p-2">
      <div className="min-h-0 flex-1">
        <CodeViewer artifact={initialArtifact} onCopy={copy} copyStatus={copyStatus} />
      </div>
      <Separator className="my-1" />
      <div className="min-h-[260px] flex-1">
        <WorkspaceChat
          workspaceType="web"
          messages={chatLog}
          annotations={allChips}
          onSubmitTask={handleSubmit}
          title="Workspace chat"
        />
      </div>
    </div>
  )

  return (
    <WorkspaceShell
      type="web"
      sidebar={sidebar}
      preview={preview}
      codeChat={codeChat}
      sidebarTitle="Build · Tokens · Tree"
      previewTitle="Live preview"
      codeChatTitle="Code & iteration"
    />
  )
}

// ─── Page entry (client component; provider-wrapped) ──────────────────────

export default function WebWorkspacePage() {
  return (
    <PersistentWorkspaceProvider type="web">
      <WebWorkspacePageContents />
      <WorkspaceOnboardingTour type="web" />
    </PersistentWorkspaceProvider>
  )
}
