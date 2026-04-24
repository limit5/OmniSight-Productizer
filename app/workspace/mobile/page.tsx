/**
 * V7 #3 (TODO row ~2690, #323) — Mobile Workspace main page.
 *
 * The Mobile sibling of `app/workspace/web/page.tsx`.  Composes the
 * three-column `WorkspaceShell` into the `/workspace/mobile` surface
 * that operators land on to iterate on a mobile app across iOS +
 * Android (+ Flutter / React-Native cross-platform toolchains):
 *
 *   ┌────────────────┬────────────────────────────┬──────────────────┐
 *   │ project tree   │  device-frame preview      │   code viewer    │
 *   │ + platform     │  + device switcher         │   + diff badge   │
 *   │   selector     │  + multi-device grid       │   + copy button  │
 *   │ + build config │    toggle                  │ ──────────────── │
 *   │                │  + visual annotator        │   workspace chat │
 *   │                │    overlay                  │                  │
 *   └────────────────┴────────────────────────────┴──────────────────┘
 *
 * Sub-bullets the row enumerates and how this page satisfies each:
 *
 *   • Left sidebar
 *       – Project tree (`MobileProjectTree`) — mirrors the Web
 *         project-tree shape: static folder view, collapsible nodes,
 *         selection emits an event only (the tree stays stateless).
 *       – Platform selector (`MobilePlatformSelector`) — `ios` /
 *         `android` / `flutter` / `react-native`.  Mirrors the
 *         `MobilePlatform` vocabulary the V7 #1 `MobileVisualAnnotator`
 *         pins on every annotation payload, so what the operator
 *         selects here is exactly what the agent skill sees.
 *       – Build config (`MobileBuildConfigEditor`) — build-variant +
 *         ABI + signing mode.  Captured as a `MobileBuildConfig`
 *         snapshot the operator can attach to the next chat prompt via
 *         "Send config to agent" (chip mirrors the Web design-token
 *         "Send tokens to agent" flow).
 *
 *   • Center pane
 *       – Device switcher (`DeviceSwitcher`) — six `DeviceProfileId`
 *         presets from V6 #3 `device-frame.tsx`.  Single source of
 *         truth — palette is built from `DEVICE_PROFILE_IDS`.
 *       – Multi-device grid toggle — switches between single-device
 *         (`DeviceFrame` + `MobileVisualAnnotator` overlay) and grid
 *         (V6 #4 `DeviceGrid` over the platform-filtered subset).
 *         Filtering by the active platform (`ios`→ios frames,
 *         `android`→android frames, `flutter/react-native`→both)
 *         keeps the grid relevant to whatever toolchain the operator
 *         chose.  The selected device is honoured in both modes —
 *         clicking a cell in the grid sets it as the active device.
 *       – Visual annotator (`MobileVisualAnnotator` from V7 #1)
 *         overlays the active device's screenshot.  Annotations
 *         surface as toggleable chat-attachment chips so the next
 *         agent prompt references them by id (matches the V7 #1
 *         payload wire shape `MobileVisualAnnotationAgentPayload`).
 *
 *   • Right pane
 *       – Code viewer (`MobileCodeViewer`) — reuses the same minimal
 *         diff classifier the Web page ships with.  File ext is
 *         derived from the active platform via
 *         `resolveFileExt(resolveFramework(platform))` so the operator
 *         sees SwiftUI `.swift` / Compose `.kt` / Flutter `.dart` /
 *         RN `.tsx` per selection.
 *       – Workspace chat (`WorkspaceChat` from V0 #7) drives the
 *         conversational iteration loop; this page feeds it the
 *         mobile-specific placeholder, the running message log, and
 *         the pending annotation / palette / build-config hints.
 *
 * Why a Client Component end-to-end:
 *   The annotator overlay, the device-switcher + grid-toggle state,
 *   the platform selector, and the chat composer all need React
 *   state.  The parent `[type]/layout.tsx` only wraps the *dynamic*
 *   route — `/workspace/mobile` is a *static* sibling so we wrap our
 *   own `<PersistentWorkspaceProvider type="mobile">` inline, same
 *   pattern the Web page uses (V0 #4 persistence without forking the
 *   persistence layer).
 *
 * Out-of-scope for V7 #3 (tracked as separate TODO rows under V7):
 *   - Build status panel (Xcode / Gradle progress + artifact link) —
 *     V7 row "Build status panel".
 *   - Store submission dashboard (App Store / Play Console status) —
 *     V7 row "Store submission dashboard".
 *   - Live SSE wire-up of `mobile_workspace.iteration_timeline.*`
 *     events from V7 #2 `backend/mobile_iteration_timeline.py`.  The
 *     page is structured so a future caller can feed a
 *     `IterationEntry[]` snapshot into a new sidebar section without
 *     reshaping the composition.
 */
"use client"

import * as React from "react"
import {
  Apple,
  Check,
  ClipboardCopy,
  ExternalLink,
  Folder,
  FolderOpen,
  Grid3x3,
  Layers,
  Send,
  Smartphone,
  SmartphoneCharging,
  Square,
} from "lucide-react"

import { cn } from "@/lib/utils"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Separator } from "@/components/ui/separator"
import { Switch } from "@/components/ui/switch"
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
import {
  WorkspaceChat,
  type WorkspaceChatAnnotation,
  type WorkspaceChatMessage,
  type WorkspaceChatSubmission,
} from "@/components/omnisight/workspace-chat"
import { useWorkspaceContext } from "@/components/omnisight/workspace-context"
import {
  DeviceFrame,
  DEVICE_PROFILE_IDS,
  DEVICE_PROFILES,
  getDeviceProfile,
  type DeviceProfile,
  type DeviceProfileId,
} from "@/components/omnisight/device-frame"
import {
  DeviceGrid,
} from "@/components/omnisight/device-grid"
import {
  MobileVisualAnnotator,
  FRAMEWORK_TO_FILE_EXT,
  MOBILE_PLATFORM_TO_FRAMEWORK,
  resolveFileExt,
  resolveFramework,
  toMobileAgentPayloads,
  type MobilePlatform,
  type MobileVisualAnnotation,
  type MobileVisualAnnotationAgentPayload,
} from "@/components/omnisight/mobile-visual-annotator"

// ─── Public shapes (exported for unit tests + storybook) ──────────────────

export interface MobileProjectTreeNode {
  id: string
  name: string
  kind: "dir" | "file"
  children?: MobileProjectTreeNode[]
}

export interface MobilePlatformOption {
  /** Stable id used in state + agent payloads. */
  id: MobilePlatform
  /** Human label rendered on the selector. */
  label: string
  /** Short descriptor — shown as the selected-state line in the sidebar. */
  description: string
}

export interface MobileBuildConfig {
  /** Debug / Release — controls signing + optimisation. */
  variant: "debug" | "release"
  /** ABI fed into `./gradlew -PtargetAbi=<abi>` / Xcode xcarchive. */
  abi: "arm64-v8a" | "armeabi-v7a" | "x86_64"
  /** Code-signing identity hint — free-text, passed into agent prompt. */
  signingIdentity: string
}

export interface MobileCodeArtifact {
  /** Stable id — usually a file path. */
  id: string
  /** Display label (e.g. `ios/App/ContentView.swift`). */
  label: string
  /** Full file body. */
  source: string
  /** Optional unified diff for the latest agent change. */
  diff?: string | null
}

// ─── Public constants (exported so tests + storybook can re-import) ───────

/**
 * Platform selector vocabulary — `id` matches the `MobilePlatform`
 * union exported by V7 #1 `mobile-visual-annotator.tsx`.  Keep this
 * order stable: UI surfaces reference it by index in snapshot tests.
 */
export const MOBILE_PLATFORM_OPTIONS: readonly MobilePlatformOption[] = Object.freeze([
  {
    id: "ios",
    label: "iOS",
    description: "SwiftUI · iPhone / iPad simulators.",
  },
  {
    id: "android",
    label: "Android",
    description: "Jetpack Compose · Pixel / Galaxy emulators.",
  },
  {
    id: "flutter",
    label: "Flutter",
    description: "Dart · cross-platform single codebase.",
  },
  {
    id: "react-native",
    label: "React Native",
    description: "TypeScript · shared RN core.",
  },
])

/** Default platform surfaced on first render — matches annotator default. */
export const DEFAULT_MOBILE_PLATFORM: MobilePlatform = "ios"

/** Default device surfaced on first render — matches annotator default. */
export const DEFAULT_MOBILE_DEVICE: DeviceProfileId = "iphone-15"

export const DEFAULT_MOBILE_PROJECT_TREE: readonly MobileProjectTreeNode[] = Object.freeze([
  {
    id: "root/ios",
    name: "ios",
    kind: "dir",
    children: [
      { id: "root/ios/App", name: "App", kind: "dir", children: [
        { id: "root/ios/App/ContentView.swift", name: "ContentView.swift", kind: "file" },
        { id: "root/ios/App/AppEntry.swift", name: "AppEntry.swift", kind: "file" },
      ]},
      { id: "root/ios/Info.plist", name: "Info.plist", kind: "file" },
    ],
  },
  {
    id: "root/android",
    name: "android",
    kind: "dir",
    children: [
      { id: "root/android/app/src/main/java/com/example/MainActivity.kt", name: "MainActivity.kt", kind: "file" },
      { id: "root/android/app/src/main/java/com/example/ui/HomeScreen.kt", name: "HomeScreen.kt", kind: "file" },
      { id: "root/android/build.gradle.kts", name: "build.gradle.kts", kind: "file" },
    ],
  },
  {
    id: "root/shared",
    name: "shared",
    kind: "dir",
    children: [
      { id: "root/shared/design_tokens.json", name: "design_tokens.json", kind: "file" },
    ],
  },
])

export const DEFAULT_MOBILE_BUILD_CONFIG: MobileBuildConfig = Object.freeze({
  variant: "debug",
  abi: "arm64-v8a",
  signingIdentity: "",
})

/** ABI options — mirrors `configs/platforms/android-*.yaml` supported ABIs. */
export const MOBILE_BUILD_ABIS: readonly MobileBuildConfig["abi"][] = Object.freeze([
  "arm64-v8a",
  "armeabi-v7a",
  "x86_64",
])

// ─── Pure helpers (exported for unit tests) ────────────────────────────────

/** Shorten a long source path for sidebar display. */
export function shortenPath(path: string, maxChars = 36): string {
  if (typeof path !== "string") return ""
  if (path.length <= maxChars) return path
  return `…${path.slice(-(maxChars - 1))}`
}

/**
 * Filter the V6 #3 device profile list to the ones that match the
 * active workspace platform.  Flutter / React-Native span both
 * platforms so they surface every device; pure iOS / Android collapse
 * to their own subset.  Pure — used by the grid filter and the device
 * switcher tabs; keeps selection deterministic across platform swaps.
 */
export function devicesForPlatform(
  platform: MobilePlatform,
): readonly DeviceProfileId[] {
  if (platform === "flutter" || platform === "react-native") {
    return DEVICE_PROFILE_IDS
  }
  return DEVICE_PROFILE_IDS.filter(
    (id) => DEVICE_PROFILES[id].platform === platform,
  )
}

/**
 * Pick a reasonable active-device id given a platform.  If the
 * current device still belongs to the filtered set, return it; else
 * fall back to the first entry (or the default when the filter is
 * empty — which should never happen with the six-preset catalogue but
 * we defend against it anyway).
 */
export function resolveActiveDevice(
  platform: MobilePlatform,
  current: DeviceProfileId,
): DeviceProfileId {
  const allowed = devicesForPlatform(platform)
  if (allowed.includes(current)) return current
  return allowed[0] ?? DEFAULT_MOBILE_DEVICE
}

/**
 * Encode an annotation as a stable, human-readable chat hint label.
 * The label feeds the `WorkspaceChatAnnotation` chip the operator
 * toggles in the composer; the structured payload
 * (`MobileVisualAnnotationAgentPayload`) rides alongside via
 * `toMobileAgentPayloads(...)` on submit.
 */
export function mobileAnnotationChipLabel(
  annotation: MobileVisualAnnotation,
): string {
  const kind = annotation.type === "rect" ? "Region" : "Pin"
  const ordinal = annotation.label ?? 0
  const summary = annotation.comment.trim().slice(0, 32)
  const suffix = summary.length > 0 ? ` — ${summary}` : ""
  return `${kind} #${ordinal}${suffix}`
}

/** Build a chip describing the live build-config snapshot. */
export function buildConfigChip(
  config: MobileBuildConfig,
): WorkspaceChatAnnotation {
  const identity = config.signingIdentity.trim()
  const identityPart = identity.length > 0 ? ` · ${identity}` : ""
  return {
    id: `build:${config.variant}:${config.abi}:${identity}`,
    label: `Build · ${config.variant} · ${config.abi}${identityPart}`,
    description: `Target variant=${config.variant}, abi=${config.abi}${
      identity.length > 0 ? `, signingIdentity=${identity}` : ""
    }.`,
  }
}

/** Build a chip for the active platform selection. */
export function platformChip(
  option: MobilePlatformOption,
): WorkspaceChatAnnotation {
  const framework = resolveFramework(option.id)
  const ext = resolveFileExt(framework)
  return {
    id: `platform:${option.id}`,
    label: `Platform · ${option.label}`,
    description: `Target ${option.description} (${framework} · ${ext}).`,
  }
}

/**
 * Naïve unified-diff line classifier — matches the Web page's shape so
 * shared snapshot tests would line up.  Empty / non-string diffs
 * return an empty list so callers can fall back to "no changes".
 */
export type DiffLineKind = "add" | "del" | "ctx" | "meta"

export interface DiffLine {
  kind: DiffLineKind
  text: string
}

export function classifyDiffLines(
  diff: string | null | undefined,
): DiffLine[] {
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

// ─── Sample artifact (V7 #3 stub — replaced once the agent wire lands) ────

const SAMPLE_MOBILE_CODE: MobileCodeArtifact = {
  id: "ios/App/ContentView.swift",
  label: "ios/App/ContentView.swift",
  source: `import SwiftUI\n\nstruct ContentView: View {\n  var body: some View {\n    VStack(spacing: 16) {\n      Text(\"Hello, OmniSight\")\n        .font(.title2)\n        .fontWeight(.semibold)\n    }\n    .padding()\n  }\n}\n`,
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

interface MobileProjectTreeProps {
  nodes: readonly MobileProjectTreeNode[]
  onSelectFile?: (node: MobileProjectTreeNode) => void
}

function MobileProjectTree({ nodes, onSelectFile }: MobileProjectTreeProps) {
  return (
    <ul data-testid="mobile-project-tree" className="flex flex-col gap-0.5 text-xs">
      {nodes.map((n) => (
        <MobileProjectTreeRow key={n.id} node={n} depth={0} onSelectFile={onSelectFile} />
      ))}
    </ul>
  )
}

interface MobileProjectTreeRowProps {
  node: MobileProjectTreeNode
  depth: number
  onSelectFile?: (node: MobileProjectTreeNode) => void
}

function MobileProjectTreeRow({ node, depth, onSelectFile }: MobileProjectTreeRowProps) {
  const [expanded, setExpanded] = React.useState<boolean>(depth === 0)
  if (node.kind === "dir") {
    return (
      <li>
        <button
          type="button"
          data-testid={`mobile-project-tree-dir-${node.id}`}
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
              <MobileProjectTreeRow
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
        data-testid={`mobile-project-tree-file-${node.id}`}
        onClick={() => onSelectFile?.(node)}
        className="flex w-full items-center gap-1.5 rounded-sm px-1.5 py-1 text-left text-muted-foreground hover:bg-accent hover:text-foreground"
        style={{ paddingLeft: `${depth * 12 + 18}px` }}
      >
        <span className="truncate">{node.name}</span>
      </button>
    </li>
  )
}

interface MobilePlatformSelectorProps {
  options: readonly MobilePlatformOption[]
  value: MobilePlatform
  onChange: (platform: MobilePlatform) => void
}

function MobilePlatformSelector({ options, value, onChange }: MobilePlatformSelectorProps) {
  return (
    <ul data-testid="mobile-platform-selector" className="flex flex-col gap-1">
      {options.map((option) => {
        const isSelected = option.id === value
        const Icon = option.id === "ios"
          ? Apple
          : option.id === "android"
            ? SmartphoneCharging
            : Smartphone
        return (
          <li key={option.id}>
            <button
              type="button"
              data-testid={`mobile-platform-option-${option.id}`}
              data-selected={isSelected ? "true" : "false"}
              aria-pressed={isSelected}
              onClick={() => onChange(option.id)}
              className={cn(
                "flex w-full items-start justify-between gap-2 rounded-md border border-border/60 px-2 py-1.5 text-left text-xs transition-colors",
                isSelected
                  ? "border-primary/60 bg-primary/10 text-foreground"
                  : "bg-background/50 text-muted-foreground hover:bg-accent hover:text-foreground",
              )}
            >
              <span className="flex flex-col gap-0.5">
                <span className="flex items-center gap-1.5 font-medium text-foreground">
                  <Icon className="size-3.5" aria-hidden="true" />
                  {option.label}
                </span>
                <span className="text-[10px] text-muted-foreground">
                  {option.description}
                </span>
              </span>
              {isSelected && (
                <Check className="size-3.5 shrink-0 text-primary" aria-hidden="true" />
              )}
            </button>
          </li>
        )
      })}
    </ul>
  )
}

interface MobileBuildConfigEditorProps {
  config: MobileBuildConfig
  onChange: (next: MobileBuildConfig) => void
  onSendToAgent: () => void
}

function MobileBuildConfigEditor({
  config,
  onChange,
  onSendToAgent,
}: MobileBuildConfigEditorProps) {
  return (
    <div data-testid="mobile-build-config-editor" className="flex flex-col gap-3 text-xs">
      <div className="flex flex-col gap-1.5">
        <Label className="text-[11px] uppercase tracking-wider">Variant</Label>
        <Select
          data-testid="mobile-build-config-variant-select"
          value={config.variant}
          onValueChange={(value) =>
            onChange({ ...config, variant: value as MobileBuildConfig["variant"] })
          }
        >
          <SelectTrigger
            data-testid="mobile-build-config-variant-trigger"
            className="h-8 text-xs"
          >
            <SelectValue placeholder="Pick variant" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem
              value="debug"
              data-testid="mobile-build-config-variant-item-debug"
            >
              Debug
            </SelectItem>
            <SelectItem
              value="release"
              data-testid="mobile-build-config-variant-item-release"
            >
              Release
            </SelectItem>
          </SelectContent>
        </Select>
      </div>

      <div className="flex flex-col gap-1.5">
        <Label className="text-[11px] uppercase tracking-wider">Target ABI</Label>
        <Select
          data-testid="mobile-build-config-abi-select"
          value={config.abi}
          onValueChange={(value) =>
            onChange({ ...config, abi: value as MobileBuildConfig["abi"] })
          }
        >
          <SelectTrigger
            data-testid="mobile-build-config-abi-trigger"
            className="h-8 text-xs"
          >
            <SelectValue placeholder="Pick ABI" />
          </SelectTrigger>
          <SelectContent>
            {MOBILE_BUILD_ABIS.map((abi) => (
              <SelectItem
                key={abi}
                value={abi}
                data-testid={`mobile-build-config-abi-item-${abi}`}
              >
                {abi}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div className="flex flex-col gap-1.5">
        <Label htmlFor="mobile-build-config-signing" className="text-[11px] uppercase tracking-wider">
          Signing identity
        </Label>
        <Input
          id="mobile-build-config-signing"
          data-testid="mobile-build-config-signing-input"
          value={config.signingIdentity}
          onChange={(e) => onChange({ ...config, signingIdentity: e.target.value })}
          placeholder="Team ID · Keystore alias"
          className="h-7 font-mono text-xs"
          spellCheck={false}
        />
      </div>

      <Button
        type="button"
        data-testid="mobile-build-config-send"
        onClick={onSendToAgent}
        size="sm"
        variant="secondary"
        className="h-7 text-xs"
      >
        <Send className="mr-1 size-3" aria-hidden="true" />
        Send config to agent
      </Button>
    </div>
  )
}

interface DeviceSwitcherProps {
  devices: readonly DeviceProfileId[]
  active: DeviceProfileId
  onChange: (id: DeviceProfileId) => void
}

function DeviceSwitcher({ devices, active, onChange }: DeviceSwitcherProps) {
  if (devices.length === 0) {
    return (
      <span
        data-testid="mobile-device-switcher-empty"
        className="text-[10px] uppercase tracking-wider text-muted-foreground"
      >
        No devices for platform
      </span>
    )
  }
  const effectiveActive = devices.includes(active) ? active : devices[0]
  return (
    <Tabs
      value={effectiveActive}
      onValueChange={(v) => onChange(v as DeviceProfileId)}
      data-testid="mobile-device-switcher"
    >
      <TabsList className="h-8">
        {devices.map((id) => {
          const profile = DEVICE_PROFILES[id]
          return (
            <TabsTrigger
              key={id}
              value={id}
              data-testid={`mobile-device-switcher-${id}`}
              className="h-7 gap-1.5 px-2 text-xs"
            >
              <Smartphone className="size-3.5" aria-hidden="true" />
              {profile.label}
            </TabsTrigger>
          )
        })}
      </TabsList>
    </Tabs>
  )
}

interface MobilePreviewSurfaceProps {
  platform: MobilePlatform
  device: DeviceProfileId
  gridMode: boolean
  screenshotUrl: string | null
  annotations: MobileVisualAnnotation[]
  onAnnotationsChange: (next: MobileVisualAnnotation[]) => void
  onSendAnnotationsToAgent: (
    payloads: MobileVisualAnnotationAgentPayload[],
  ) => void
  onSelectDevice: (id: DeviceProfileId) => void
  deviceSubset: readonly DeviceProfileId[]
}

function MobilePreviewSurface({
  platform,
  device,
  gridMode,
  screenshotUrl,
  annotations,
  onAnnotationsChange,
  onSendAnnotationsToAgent,
  onSelectDevice,
  deviceSubset,
}: MobilePreviewSurfaceProps) {
  return (
    <div
      data-testid="mobile-preview-surface"
      data-platform={platform}
      data-device={device}
      data-grid-mode={gridMode ? "true" : "false"}
      className="flex h-full min-h-0 w-full items-start justify-center overflow-auto bg-muted/30 p-4"
    >
      {gridMode ? (
        <DeviceGrid
          data-testid="mobile-preview-device-grid"
          devices={deviceSubset}
          screenshotUrl={screenshotUrl ?? undefined}
          empty={!screenshotUrl}
          frameWidth={200}
          selectedDevice={device}
          onSelectDevice={onSelectDevice}
          title="Multi-device preview"
          description="Same screen rendered across the active platform's devices. Click a frame to select it."
          emptyLabel="No device matches the active platform."
        />
      ) : screenshotUrl ? (
        <MobileVisualAnnotator
          data-testid="mobile-preview-annotator"
          screenshotUrl={screenshotUrl}
          screenshotAlt={`${DEVICE_PROFILES[device].label} preview`}
          device={device}
          platform={platform}
          annotations={annotations}
          onAnnotationsChange={onAnnotationsChange}
          onSendToAgent={onSendAnnotationsToAgent}
          frameWidth={320}
        />
      ) : (
        <div
          data-testid="mobile-preview-empty"
          className="flex min-h-[260px] flex-col items-center justify-center gap-2 text-muted-foreground"
        >
          <DeviceFrame
            device={device}
            empty
            showLabel
            data-testid="mobile-preview-empty-frame"
            width={220}
          />
          <p className="text-xs">
            No simulator screenshot yet — describe the screen to start.
          </p>
        </div>
      )}
    </div>
  )
}

interface MobileCodeViewerProps {
  artifact: MobileCodeArtifact
  platformFileExt: string
  onCopy: (text: string) => Promise<void> | void
  copyStatus: "idle" | "copied" | "error"
}

function MobileCodeViewer({
  artifact,
  platformFileExt,
  onCopy,
  copyStatus,
}: MobileCodeViewerProps) {
  const diffLines = React.useMemo(() => classifyDiffLines(artifact.diff ?? null), [
    artifact.diff,
  ])
  return (
    <article
      data-testid="mobile-code-viewer"
      data-artifact-id={artifact.id}
      data-platform-ext={platformFileExt}
      className="flex min-h-0 flex-col overflow-hidden rounded-md border border-border bg-background"
    >
      <header className="flex items-center justify-between gap-2 border-b border-border bg-muted/40 px-2 py-1">
        <div className="flex items-center gap-1.5 text-xs font-medium">
          <ExternalLink className="size-3.5 text-muted-foreground" aria-hidden="true" />
          <span data-testid="mobile-code-viewer-label" className="truncate">
            {shortenPath(artifact.label)}
          </span>
          <Badge
            data-testid="mobile-code-viewer-ext-badge"
            variant="outline"
            className="ml-1 h-4 px-1.5 text-[10px] font-mono"
          >
            {platformFileExt}
          </Badge>
          {diffLines.length > 0 && (
            <Badge
              data-testid="mobile-code-viewer-diff-badge"
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
          data-testid="mobile-code-viewer-copy"
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
      <div data-testid="mobile-code-viewer-body" className="min-h-0 flex-1 overflow-auto">
        {diffLines.length > 0 ? (
          <pre
            data-testid="mobile-code-viewer-diff"
            className="m-0 whitespace-pre overflow-x-auto p-2 font-mono text-[11px] leading-snug"
          >
            {diffLines.map((line, idx) => (
              <span
                key={idx}
                data-testid={`mobile-code-viewer-diff-line-${idx}`}
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
            data-testid="mobile-code-viewer-source"
            className="m-0 whitespace-pre overflow-x-auto p-2 font-mono text-[11px] leading-snug text-foreground"
          >
            {artifact.source}
          </pre>
        )}
      </div>
    </article>
  )
}

// ─── Page contents (read provider; mount visual surfaces) ─────────────────

export interface MobileWorkspacePageContentsProps {
  /** Initial platform — exposed so tests and storybook can pin it. */
  initialPlatform?: MobilePlatform
  /** Initial device — honoured unless the platform filter excludes it. */
  initialDevice?: DeviceProfileId
  /** Initial build-config snapshot. */
  initialBuildConfig?: MobileBuildConfig
  /** Sample code artifact shown until the agent wire lands. */
  initialArtifact?: MobileCodeArtifact
  /** Project tree to render in the sidebar. */
  projectTree?: readonly MobileProjectTreeNode[]
  /** Start in multi-device grid mode. */
  initialGridMode?: boolean
  /**
   * Preview screenshot URL override — otherwise read from the workspace
   * provider (`preview.url`).  Exposed so storybook / tests can pin a
   * deterministic image without hydrating the provider.
   */
  screenshotUrlOverride?: string | null
  /**
   * Inject a clipboard writer in tests (jsdom has no `navigator.clipboard`
   * by default).  Defaults to `navigator.clipboard.writeText`.
   */
  copyToClipboardImpl?: (text: string) => Promise<void>
  /**
   * Test seam: forward agent-bound submissions out of the page so tests
   * can assert composer payload shape without mocking the backend.
   * Receives the structured mobile annotation payload list alongside
   * the base submission (same pattern as the Web page).
   */
  onAgentSubmit?: (
    submission: WorkspaceChatSubmission & {
      mobileAnnotationPayloads: MobileVisualAnnotationAgentPayload[]
      platform: MobilePlatform
      device: DeviceProfileId
      buildConfig: MobileBuildConfig
    },
  ) => void | Promise<void>
}

export function MobileWorkspacePageContents({
  initialPlatform = DEFAULT_MOBILE_PLATFORM,
  initialDevice = DEFAULT_MOBILE_DEVICE,
  initialBuildConfig = DEFAULT_MOBILE_BUILD_CONFIG,
  initialArtifact = SAMPLE_MOBILE_CODE,
  projectTree = DEFAULT_MOBILE_PROJECT_TREE,
  initialGridMode = false,
  screenshotUrlOverride,
  copyToClipboardImpl,
  onAgentSubmit,
}: MobileWorkspacePageContentsProps) {
  const ctx = useWorkspaceContext()
  const [platform, setPlatform] = React.useState<MobilePlatform>(initialPlatform)
  const [device, setDevice] = React.useState<DeviceProfileId>(initialDevice)
  const [gridMode, setGridMode] = React.useState<boolean>(initialGridMode)
  const [buildConfig, setBuildConfig] = React.useState<MobileBuildConfig>(initialBuildConfig)
  const [buildConfigSnapshot, setBuildConfigSnapshot] =
    React.useState<MobileBuildConfig | null>(null)
  const [annotations, setAnnotations] = React.useState<MobileVisualAnnotation[]>([])
  const [chatLog, setChatLog] = React.useState<WorkspaceChatMessage[]>([])
  const [copyStatus, setCopyStatus] = React.useState<"idle" | "copied" | "error">("idle")

  // Keep device valid when platform changes — if the current device
  // doesn't belong to the new platform's subset, fall back to its
  // first allowed entry.  Pure derivation inside an effect because the
  // state it writes feeds child components; a render-time swap would
  // tear a controlled-to-derived gap on DeviceGrid's `selectedDevice`.
  React.useEffect(() => {
    setDevice((current) => resolveActiveDevice(platform, current))
  }, [platform])

  const deviceSubset = React.useMemo(() => devicesForPlatform(platform), [platform])
  const activeProfile: DeviceProfile = React.useMemo(
    () => getDeviceProfile(device),
    [device],
  )

  const framework = React.useMemo(() => resolveFramework(platform), [platform])
  const fileExt = React.useMemo(() => resolveFileExt(framework), [framework])

  const sendBuildConfigToAgent = React.useCallback(() => {
    setBuildConfigSnapshot({ ...buildConfig })
  }, [buildConfig])

  // Annotation chips, platform chip, and (optionally) a build-config
  // chip collectively form the chat-composer's available references.
  const annotationChips = React.useMemo<WorkspaceChatAnnotation[]>(
    () =>
      annotations.map((ann) => ({
        id: `annotation:${ann.id}`,
        label: mobileAnnotationChipLabel(ann),
        description: ann.comment.length > 0 ? ann.comment : undefined,
      })),
    [annotations],
  )

  const platformChipValue = React.useMemo<WorkspaceChatAnnotation>(() => {
    const option =
      MOBILE_PLATFORM_OPTIONS.find((o) => o.id === platform) ?? MOBILE_PLATFORM_OPTIONS[0]
    return platformChip(option)
  }, [platform])

  const buildChips = React.useMemo<WorkspaceChatAnnotation[]>(
    () => (buildConfigSnapshot ? [buildConfigChip(buildConfigSnapshot)] : []),
    [buildConfigSnapshot],
  )

  const allChips = React.useMemo<WorkspaceChatAnnotation[]>(
    () => [platformChipValue, ...annotationChips, ...buildChips],
    [platformChipValue, annotationChips, buildChips],
  )

  const handleSubmit = React.useCallback(
    async (submission: WorkspaceChatSubmission) => {
      const selectedAnnotations = annotations.filter((a) =>
        submission.annotationIds.includes(`annotation:${a.id}`),
      )
      const mobileAnnotationPayloads = toMobileAgentPayloads(
        selectedAnnotations,
        platform,
        device,
      )
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
          mobileAnnotationPayloads,
          platform,
          device,
          buildConfig,
        })
      } finally {
        // Build-config snapshot is one-shot per submit so the chip clears;
        // annotations stay selected so the operator can iterate.
        setBuildConfigSnapshot(null)
      }
    },
    [annotations, buildConfig, ctx, device, onAgentSubmit, platform],
  )

  const handleSendAnnotationsToAgent = React.useCallback(
    (_payloads: MobileVisualAnnotationAgentPayload[]) => {
      // The annotator fires this when the operator clicks "Send to agent"
      // inside the overlay.  The Mobile page deliberately does not POST
      // on its own — the operator still has to hit the chat composer's
      // submit, where the same payloads travel via `annotationIds`.
      // Kept as a noop hook so host tests can spy on it.
      void _payloads
    },
    [],
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

  const screenshotUrl =
    screenshotUrlOverride !== undefined ? screenshotUrlOverride : ctx.preview.url

  const sidebar = (
    <div data-testid="mobile-workspace-sidebar" className="flex flex-col">
      <SidebarSection
        title="Project tree"
        icon={<Folder className="size-3.5" aria-hidden="true" />}
        testId="mobile-sidebar-section-tree"
      >
        <MobileProjectTree nodes={projectTree} />
      </SidebarSection>
      <SidebarSection
        title="Platform"
        icon={<Layers className="size-3.5" aria-hidden="true" />}
        testId="mobile-sidebar-section-platform"
      >
        <MobilePlatformSelector
          options={MOBILE_PLATFORM_OPTIONS}
          value={platform}
          onChange={setPlatform}
        />
      </SidebarSection>
      <SidebarSection
        title="Build config"
        icon={<Square className="size-3.5" aria-hidden="true" />}
        testId="mobile-sidebar-section-build"
      >
        <MobileBuildConfigEditor
          config={buildConfig}
          onChange={setBuildConfig}
          onSendToAgent={sendBuildConfigToAgent}
        />
      </SidebarSection>
    </div>
  )

  const preview = (
    <div data-testid="mobile-workspace-preview" className="flex h-full min-h-0 flex-col">
      <div className="flex h-9 shrink-0 items-center justify-between gap-2 border-b border-border bg-background/40 px-3">
        <DeviceSwitcher
          devices={deviceSubset}
          active={device}
          onChange={setDevice}
        />
        <div className="flex items-center gap-3">
          <span
            data-testid="mobile-preview-native-size"
            className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground"
          >
            {activeProfile.screenWidth}×{activeProfile.screenHeight}
          </span>
          <label
            htmlFor="mobile-preview-grid-toggle"
            className="flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-muted-foreground"
          >
            <Grid3x3 className="size-3.5" aria-hidden="true" />
            Grid
            <Switch
              id="mobile-preview-grid-toggle"
              data-testid="mobile-preview-grid-toggle"
              checked={gridMode}
              onCheckedChange={setGridMode}
            />
          </label>
        </div>
      </div>
      <div className="min-h-0 flex-1">
        <MobilePreviewSurface
          platform={platform}
          device={device}
          gridMode={gridMode}
          screenshotUrl={screenshotUrl}
          annotations={annotations}
          onAnnotationsChange={setAnnotations}
          onSendAnnotationsToAgent={handleSendAnnotationsToAgent}
          onSelectDevice={setDevice}
          deviceSubset={deviceSubset}
        />
      </div>
    </div>
  )

  const codeChat = (
    <div data-testid="mobile-workspace-code-chat" className="flex h-full min-h-0 flex-col gap-2 p-2">
      <div className="min-h-0 flex-1">
        <MobileCodeViewer
          artifact={initialArtifact}
          platformFileExt={fileExt}
          onCopy={copy}
          copyStatus={copyStatus}
        />
      </div>
      <Separator className="my-1" />
      <div className="min-h-[260px] flex-1">
        <WorkspaceChat
          workspaceType="mobile"
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
      type="mobile"
      sidebar={sidebar}
      preview={preview}
      codeChat={codeChat}
      sidebarTitle="Tree · Platform · Build"
      previewTitle="Device preview"
      codeChatTitle="Code & iteration"
    />
  )
}

// ─── Page entry (client component; provider-wrapped) ──────────────────────

export default function MobileWorkspacePage() {
  return (
    <PersistentWorkspaceProvider type="mobile">
      <MobileWorkspacePageContents />
    </PersistentWorkspaceProvider>
  )
}

// Re-export the platform/framework vocabulary + device catalogue from
// the underlying components so callers importing this page's module
// (storybook, unit tests, future router) don't need to reach into the
// lower-level files — same pattern as the Web page re-exporting its
// DiffLine helpers.
export {
  DEVICE_PROFILE_IDS,
  DEVICE_PROFILES,
  FRAMEWORK_TO_FILE_EXT,
  MOBILE_PLATFORM_TO_FRAMEWORK,
  resolveFileExt,
  resolveFramework,
}
export type { DeviceProfileId, MobilePlatform, MobileVisualAnnotation }
