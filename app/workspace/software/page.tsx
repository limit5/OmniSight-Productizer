/**
 * V8 #1 (TODO row ~2699, #324) — Software Workspace main page.
 *
 * The Software sibling of `app/workspace/web/page.tsx` and
 * `app/workspace/mobile/page.tsx`.  Composes the three-column
 * `WorkspaceShell` into the `/workspace/software` surface that
 * operators land on to iterate on a backend / library / CLI / service
 * across Python, TypeScript, Go, Rust, Java, C++ toolchains:
 *
 *   ┌─────────────────┬───────────────────────────┬──────────────────┐
 *   │ project tree    │  terminal output viewer   │   code viewer    │
 *   │ + language /    │  ── or ──                 │   + diff badge   │
 *   │   framework     │  OpenAPI / Swagger        │   + copy button  │
 *   │   selector      │   interactive docs        │ ──────────────── │
 *   │ + build target  │   viewer                  │   workspace chat │
 *   │   selector      │  (toggle in the header)   │                  │
 *   └─────────────────┴───────────────────────────┴──────────────────┘
 *
 * Sub-bullets the row enumerates and how this page satisfies each:
 *
 *   • Left sidebar
 *       – Project tree (`SoftwareProjectTree`) — mirrors the Web /
 *         Mobile project-tree shape: static folder view, collapsible
 *         nodes, selection emits an event only (the tree stays
 *         stateless).
 *       – Language / framework selector (`LanguageFrameworkSelector`)
 *         — `python` / `typescript` / `go` / `rust` / `java` / `cpp`,
 *         with a per-language framework `Select` (FastAPI vs Flask vs
 *         Django, Express vs Fastify vs NestJS, …).  The chosen
 *         (language, framework) pair is exposed to the chat composer
 *         as the `language` chip so the agent gets the toolchain hint
 *         on every submit.
 *       – Build target selector (`BuildTargetSelector`) — Docker /
 *         Helm / .deb / .rpm / .msi / .dmg / wheel / npm / jar /
 *         native binary.  Captured as a `BuildTarget` snapshot the
 *         operator can attach to the next chat prompt via "Send
 *         target to agent" (chip mirrors the Web design-token / Mobile
 *         build-config "Send … to agent" flow).  The selector also
 *         filters by what the active language can plausibly produce
 *         (e.g. Python won't surface .jar) — keeps the picker
 *         self-relevant while staying advisory only.
 *
 *   • Center pane
 *       – Terminal output viewer (`TerminalOutputViewer`) — renders
 *         agent bash-tool output as a colour-coded stream.  Each line
 *         carries a `stream` tag (`build` / `test` / `deploy`) and a
 *         `severity` (`stdout` / `stderr` / `info` / `error`) so the
 *         operator can filter by stream and so stderr lines render in
 *         red.  Auto-scroll-to-bottom is on by default and toggled
 *         from the header — turning it off lets the operator review
 *         earlier output without the stream yanking the scrollbar.
 *       – OpenAPI / Swagger interactive docs viewer
 *         (`OpenApiDocsViewer`) — the alternate center-pane tab.
 *         Renders a minimal OpenAPI v3 spec (`info.title` /
 *         `info.version` / `paths.*`) as a collapsible endpoint list
 *         with HTTP-method badges (GET=sky, POST=emerald, PUT=amber,
 *         DELETE=rose, PATCH=violet).  Clicking an endpoint expands
 *         its `summary` / `description` / `parameters` (name + in +
 *         required + schema-type).  Heavy dependencies (swagger-ui-react,
 *         redoc) deliberately *not* pulled in — the LangChain firewall
 *         keeps build size lean and the V0 #2 grid wants the column to
 *         stay narrow.
 *
 *   • Right pane
 *       – Code viewer (`SoftwareCodeViewer`) — reuses the same
 *         minimal diff classifier the Web / Mobile pages ship with.
 *         File extension is derived from the active language via
 *         `LANGUAGE_FILE_EXT` so the viewer can stamp it as a small
 *         badge (parallels the Mobile `platformFileExt` badge).
 *       – Workspace chat (`WorkspaceChat` from V0 #7) drives the
 *         conversational iteration loop; this page feeds it the
 *         software-specific placeholder (already wired in
 *         `workspace-chat.tsx::PER_TYPE_PLACEHOLDER`), the running
 *         message log, and the pending language / build-target /
 *         endpoint hints.
 *
 * Why a Client Component end-to-end:
 *   The terminal-stream auto-scroll, the centre-pane tab toggle, the
 *   language / framework / target dropdowns, the OpenAPI expand-toggle,
 *   and the chat composer all need React state.  The parent
 *   `[type]/layout.tsx` only wraps the *dynamic* route —
 *   `/workspace/software` is a *static* sibling so we wrap our own
 *   `<PersistentWorkspaceProvider type="software">` inline, same
 *   pattern the Web / Mobile pages use.
 *
 * Out-of-scope for V8 #1 (tracked as separate TODO rows under V8):
 *   - Multi-platform release dashboard (artifact grid + download
 *     links) — V8 row "Multi-platform release dashboard".
 *   - Test coverage viewer (per-file bar + uncovered line highlight)
 *     — V8 row "Test coverage viewer".
 *   - Live SSE wire-up of `software_workspace.terminal_stream.*`
 *     events.  The page is structured so a future caller can pump a
 *     `TerminalLine[]` snapshot into `initialTerminalLines` (or a
 *     parent-controlled state) without reshaping the composition.
 */
"use client"

import * as React from "react"
import {
  Boxes,
  Check,
  ClipboardCopy,
  Code2,
  ExternalLink,
  FileCode,
  Folder,
  FolderOpen,
  Layers,
  Package,
  Send,
  Terminal,
} from "lucide-react"

import { cn } from "@/lib/utils"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
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
import { WorkspaceOnboardingTour } from "@/components/omnisight/workspace-onboarding-tour"
import {
  WorkspaceChat,
  type WorkspaceChatAnnotation,
  type WorkspaceChatMessage,
  type WorkspaceChatSubmission,
} from "@/components/omnisight/workspace-chat"
import { useWorkspaceContext } from "@/components/omnisight/workspace-context"

// ─── Public shapes (exported for unit tests + storybook) ──────────────────

/** Stable language vocabulary — kept narrow + stable for snapshot tests. */
export type SoftwareLanguage =
  | "python"
  | "typescript"
  | "go"
  | "rust"
  | "java"
  | "cpp"

/** Build-target vocabulary the selector exposes. */
export type BuildTarget =
  | "docker"
  | "helm"
  | "deb"
  | "rpm"
  | "msi"
  | "dmg"
  | "wheel"
  | "npm"
  | "jar"
  | "binary"

/** Center-pane tab — mutually exclusive between terminal and docs. */
export type SoftwareCenterTab = "terminal" | "openapi"

/**
 * Stream tag attached to each terminal line.  Lets the operator filter
 * by what produced the output (compile vs pytest vs `kubectl apply`).
 */
export type TerminalStream = "build" | "test" | "deploy"

/**
 * Severity classification — the viewer colours stderr / error red,
 * stdout neutral, info muted.  Mirrors `subprocess.PIPE` semantics so
 * the future SSE wire only needs to forward the tag verbatim.
 */
export type TerminalSeverity = "stdout" | "stderr" | "info" | "error"

export interface ProjectTreeNode {
  id: string
  name: string
  kind: "dir" | "file"
  children?: ProjectTreeNode[]
}

export interface LanguageOption {
  /** Stable id — flows into the agent-bound chip and chat hints. */
  id: SoftwareLanguage
  /** Human label rendered on the selector. */
  label: string
  /** Short descriptor — shown as the selected-state line in the sidebar. */
  description: string
  /** Frameworks the operator can pick for this language. */
  frameworks: readonly FrameworkOption[]
}

export interface FrameworkOption {
  /** Stable id — `${language}:${framework}` collisions are avoided. */
  id: string
  /** Human label rendered in the framework dropdown. */
  label: string
  /** Build targets this framework can plausibly emit (subset of `BuildTarget`). */
  defaultTargets: readonly BuildTarget[]
}

export interface BuildTargetOption {
  /** Stable id — flows into the agent-bound chip. */
  id: BuildTarget
  /** Human label rendered on the selector. */
  label: string
  /** Short descriptor — shown alongside the label. */
  description: string
}

export interface TerminalLine {
  /** Stable id — usually a monotonic counter or uuid. */
  id: string
  /** ISO-8601 timestamp the line was captured. */
  ts: string
  /** Which agent stream produced this line. */
  stream: TerminalStream
  /** stdout / stderr / info / error classification. */
  severity: TerminalSeverity
  /** Raw line text — *not* parsed for ANSI escapes here (the future
   *  SSE producer is expected to strip them server-side). */
  text: string
}

/**
 * Minimal OpenAPI v3 typed surface — only the fields the viewer
 * actually renders.  Full spec compatibility is intentionally not a
 * goal; callers feed in the parsed spec object directly so this stays
 * a render concern, not a validation concern.
 */
export interface OpenApiSpec {
  openapi?: string
  info?: { title?: string; version?: string; description?: string }
  paths?: Record<string, OpenApiPathItem>
}

export interface OpenApiPathItem {
  get?: OpenApiOperation
  post?: OpenApiOperation
  put?: OpenApiOperation
  delete?: OpenApiOperation
  patch?: OpenApiOperation
  options?: OpenApiOperation
  head?: OpenApiOperation
  trace?: OpenApiOperation
}

export interface OpenApiOperation {
  summary?: string
  description?: string
  operationId?: string
  tags?: string[]
  parameters?: OpenApiParameter[]
}

export interface OpenApiParameter {
  name: string
  in: "query" | "header" | "path" | "cookie"
  required?: boolean
  schema?: { type?: string; format?: string }
  description?: string
}

/** Flattened endpoint row used by the docs viewer + endpoint chip. */
export interface OpenApiEndpoint {
  method: "get" | "post" | "put" | "delete" | "patch" | "options" | "head" | "trace"
  path: string
  operation: OpenApiOperation
}

export interface SoftwareCodeArtifact {
  /** Stable id — usually a file path. */
  id: string
  /** Display label (e.g. `app/api/users.py`). */
  label: string
  /** Full file body. */
  source: string
  /** Optional unified diff for the latest agent change. */
  diff?: string | null
}

// ─── Public constants (exported so tests + storybook can re-import) ───────

/**
 * Language → file extension lookup.  Used by the code viewer's badge so
 * the operator can see which toolchain the active artifact belongs to.
 * Mirrors the file-ext convention the V7 #1 mobile annotator uses.
 */
export const LANGUAGE_FILE_EXT: Record<SoftwareLanguage, string> = Object.freeze({
  python: ".py",
  typescript: ".ts",
  go: ".go",
  rust: ".rs",
  java: ".java",
  cpp: ".cpp",
})

/**
 * Language catalogue — order is stable so snapshot tests can reference
 * by index.  Frameworks within each language are listed in the order
 * the operator most commonly reaches for them.
 */
export const LANGUAGE_OPTIONS: readonly LanguageOption[] = Object.freeze([
  {
    id: "python",
    label: "Python",
    description: "FastAPI · Flask · Django · pytest.",
    frameworks: [
      { id: "python:fastapi", label: "FastAPI", defaultTargets: ["docker", "helm", "wheel"] },
      { id: "python:flask", label: "Flask", defaultTargets: ["docker", "wheel"] },
      { id: "python:django", label: "Django", defaultTargets: ["docker", "helm", "wheel"] },
      { id: "python:cli", label: "CLI (Click/Typer)", defaultTargets: ["wheel", "binary"] },
    ],
  },
  {
    id: "typescript",
    label: "TypeScript",
    description: "Node · Express · Fastify · NestJS · Vitest.",
    frameworks: [
      { id: "ts:express", label: "Express", defaultTargets: ["docker", "helm", "npm"] },
      { id: "ts:fastify", label: "Fastify", defaultTargets: ["docker", "helm", "npm"] },
      { id: "ts:nestjs", label: "NestJS", defaultTargets: ["docker", "helm", "npm"] },
      { id: "ts:cli", label: "CLI (commander)", defaultTargets: ["npm", "binary"] },
    ],
  },
  {
    id: "go",
    label: "Go",
    description: "Echo · Gin · Chi · cobra CLI.",
    frameworks: [
      { id: "go:echo", label: "Echo", defaultTargets: ["docker", "helm", "binary"] },
      { id: "go:gin", label: "Gin", defaultTargets: ["docker", "helm", "binary"] },
      { id: "go:chi", label: "Chi", defaultTargets: ["docker", "helm", "binary"] },
      { id: "go:cli", label: "CLI (cobra)", defaultTargets: ["binary", "deb", "rpm"] },
    ],
  },
  {
    id: "rust",
    label: "Rust",
    description: "Axum · Actix · clap CLI.",
    frameworks: [
      { id: "rust:axum", label: "Axum", defaultTargets: ["docker", "helm", "binary"] },
      { id: "rust:actix", label: "Actix Web", defaultTargets: ["docker", "helm", "binary"] },
      { id: "rust:cli", label: "CLI (clap)", defaultTargets: ["binary", "deb", "rpm"] },
    ],
  },
  {
    id: "java",
    label: "Java",
    description: "Spring Boot · Quarkus · Micronaut.",
    frameworks: [
      { id: "java:spring", label: "Spring Boot", defaultTargets: ["docker", "helm", "jar"] },
      { id: "java:quarkus", label: "Quarkus", defaultTargets: ["docker", "helm", "jar"] },
      { id: "java:micronaut", label: "Micronaut", defaultTargets: ["docker", "helm", "jar"] },
    ],
  },
  {
    id: "cpp",
    label: "C++",
    description: "Drogon · Pistache · CMake binary.",
    frameworks: [
      { id: "cpp:drogon", label: "Drogon", defaultTargets: ["docker", "binary", "deb"] },
      { id: "cpp:pistache", label: "Pistache", defaultTargets: ["docker", "binary", "deb"] },
      { id: "cpp:bin", label: "Native binary", defaultTargets: ["binary", "deb", "rpm", "msi", "dmg"] },
    ],
  },
])

/**
 * Build-target catalogue.  Order matches the multi-platform release
 * dashboard grid order (Docker / Helm / .deb / .rpm / .msi / .dmg /
 * wheel / npm / jar / native binary) so snapshot diffs stay stable.
 */
export const BUILD_TARGET_OPTIONS: readonly BuildTargetOption[] = Object.freeze([
  { id: "docker", label: "Docker image", description: "OCI image · linux/amd64+arm64." },
  { id: "helm", label: "Helm chart", description: "k8s install bundle." },
  { id: "deb", label: ".deb", description: "Debian / Ubuntu package." },
  { id: "rpm", label: ".rpm", description: "RHEL / Fedora package." },
  { id: "msi", label: ".msi", description: "Windows installer." },
  { id: "dmg", label: ".dmg", description: "macOS disk image." },
  { id: "wheel", label: "Python wheel", description: "PyPI distribution." },
  { id: "npm", label: "npm package", description: "tarball + registry publish." },
  { id: "jar", label: "Java jar", description: "Executable JVM bundle." },
  { id: "binary", label: "Native binary", description: "Single static executable." },
])

export const TERMINAL_STREAMS: readonly TerminalStream[] = Object.freeze([
  "build",
  "test",
  "deploy",
])

export const DEFAULT_PROJECT_TREE: readonly ProjectTreeNode[] = Object.freeze([
  {
    id: "root/src",
    name: "src",
    kind: "dir",
    children: [
      { id: "root/src/main.py", name: "main.py", kind: "file" },
      {
        id: "root/src/api",
        name: "api",
        kind: "dir",
        children: [
          { id: "root/src/api/users.py", name: "users.py", kind: "file" },
          { id: "root/src/api/health.py", name: "health.py", kind: "file" },
        ],
      },
    ],
  },
  {
    id: "root/tests",
    name: "tests",
    kind: "dir",
    children: [
      { id: "root/tests/test_users.py", name: "test_users.py", kind: "file" },
      { id: "root/tests/test_health.py", name: "test_health.py", kind: "file" },
    ],
  },
  { id: "root/Dockerfile", name: "Dockerfile", kind: "file" },
  { id: "root/pyproject.toml", name: "pyproject.toml", kind: "file" },
])

/** Default language surfaced on first render — Python because the V0 #4
 *  persistence layer ships a Python backend itself. */
export const DEFAULT_LANGUAGE: SoftwareLanguage = "python"

/** Default build target surfaced on first render — Docker is the
 *  cross-platform lowest common denominator. */
export const DEFAULT_BUILD_TARGET: BuildTarget = "docker"

/** Default centre tab — terminal output is the primary surface. */
export const DEFAULT_CENTER_TAB: SoftwareCenterTab = "terminal"

/** Sample OpenAPI spec rendered until the agent wire feeds in a real one. */
export const SAMPLE_OPENAPI_SPEC: OpenApiSpec = Object.freeze<OpenApiSpec>({
  openapi: "3.0.3",
  info: {
    title: "OmniSight Sample Service",
    version: "0.1.0",
    description: "Reference spec rendered by the V8 #1 software workspace.",
  },
  paths: {
    "/health": {
      get: {
        summary: "Liveness probe",
        description: "Returns 200 OK when the service is up.",
        operationId: "getHealth",
        tags: ["meta"],
      },
    },
    "/users": {
      get: {
        summary: "List users",
        description: "Returns a paginated list of users.",
        operationId: "listUsers",
        tags: ["users"],
        parameters: [
          {
            name: "limit",
            in: "query",
            required: false,
            schema: { type: "integer" },
            description: "Maximum rows to return (default 50).",
          },
          {
            name: "cursor",
            in: "query",
            required: false,
            schema: { type: "string" },
            description: "Opaque pagination cursor.",
          },
        ],
      },
      post: {
        summary: "Create user",
        description: "Provisions a new user account.",
        operationId: "createUser",
        tags: ["users"],
      },
    },
    "/users/{id}": {
      get: {
        summary: "Get user",
        operationId: "getUser",
        tags: ["users"],
        parameters: [
          {
            name: "id",
            in: "path",
            required: true,
            schema: { type: "string" },
            description: "User identifier.",
          },
        ],
      },
      delete: {
        summary: "Delete user",
        operationId: "deleteUser",
        tags: ["users"],
        parameters: [
          {
            name: "id",
            in: "path",
            required: true,
            schema: { type: "string" },
            description: "User identifier.",
          },
        ],
      },
    },
  },
})

// ─── Pure helpers (exported for unit tests) ───────────────────────────────

/** Shorten a long source path for sidebar / code-viewer header display. */
export function shortenPath(path: string, maxChars = 36): string {
  if (typeof path !== "string") return ""
  if (path.length <= maxChars) return path
  return `…${path.slice(-(maxChars - 1))}`
}

/** Look up a language option by id; falls back to default. */
export function resolveLanguage(id: string | null | undefined): LanguageOption {
  const found = LANGUAGE_OPTIONS.find((opt) => opt.id === id)
  return found ?? LANGUAGE_OPTIONS[0]
}

/**
 * Resolve a framework id against the active language's catalogue.  If
 * the supplied id no longer belongs to the language (operator just
 * swapped languages), fall back to the language's first framework.
 */
export function resolveFramework(
  language: SoftwareLanguage,
  frameworkId: string | null | undefined,
): FrameworkOption {
  const lang = resolveLanguage(language)
  const found = lang.frameworks.find((f) => f.id === frameworkId)
  return found ?? lang.frameworks[0]
}

/**
 * Filter the global build-target catalogue down to what the active
 * framework can plausibly emit.  Pure — used by the build-target
 * dropdown so the picker stays self-relevant on language swaps.
 */
export function targetsForFramework(
  framework: FrameworkOption,
): readonly BuildTargetOption[] {
  const allowed = new Set<BuildTarget>(framework.defaultTargets)
  return BUILD_TARGET_OPTIONS.filter((opt) => allowed.has(opt.id))
}

/**
 * Snap the active build target to one the framework supports.  If the
 * current selection is already valid we keep it; otherwise we fall
 * back to the framework's first default — preventing a stale
 * "language=Python, target=.jar" combo after a language swap.
 */
export function resolveActiveBuildTarget(
  framework: FrameworkOption,
  current: BuildTarget,
): BuildTarget {
  const allowed = new Set<BuildTarget>(framework.defaultTargets)
  if (allowed.has(current)) return current
  return framework.defaultTargets[0] ?? DEFAULT_BUILD_TARGET
}

/** Look up the file extension that goes with a language. */
export function languageFileExt(language: SoftwareLanguage): string {
  return LANGUAGE_FILE_EXT[language] ?? ""
}

/**
 * Flatten an OpenAPI spec into an ordered endpoint list.  Pure — the
 * docs viewer iterates this directly and the endpoint chip references
 * an entry by `${method}:${path}` id.  Order: paths in spec order,
 * methods in `OPENAPI_METHOD_ORDER`.
 */
export const OPENAPI_METHOD_ORDER: readonly OpenApiEndpoint["method"][] = Object.freeze([
  "get",
  "post",
  "put",
  "patch",
  "delete",
  "options",
  "head",
  "trace",
])

export function flattenOpenApiSpec(spec: OpenApiSpec | null | undefined): OpenApiEndpoint[] {
  if (!spec || typeof spec !== "object" || !spec.paths) return []
  const out: OpenApiEndpoint[] = []
  for (const [path, item] of Object.entries(spec.paths)) {
    if (!item || typeof item !== "object") continue
    for (const method of OPENAPI_METHOD_ORDER) {
      const op = (item as OpenApiPathItem)[method]
      if (op && typeof op === "object") {
        out.push({ method, path, operation: op })
      }
    }
  }
  return out
}

/** Stable id for an endpoint chip — `${method.toUpperCase()} ${path}`. */
export function endpointId(endpoint: OpenApiEndpoint): string {
  return `endpoint:${endpoint.method}:${endpoint.path}`
}

/** Build a chip describing a single OpenAPI endpoint selection. */
export function endpointChip(endpoint: OpenApiEndpoint): WorkspaceChatAnnotation {
  const method = endpoint.method.toUpperCase()
  const summary = endpoint.operation.summary?.trim()
  return {
    id: endpointId(endpoint),
    label: `${method} ${endpoint.path}`,
    description: summary && summary.length > 0 ? summary : `OpenAPI operation ${method} ${endpoint.path}`,
  }
}

/** Build a chip describing the active language + framework. */
export function languageChip(
  language: LanguageOption,
  framework: FrameworkOption,
): WorkspaceChatAnnotation {
  return {
    id: `language:${language.id}:${framework.id}`,
    label: `Lang · ${language.label} · ${framework.label}`,
    description: `Target ${language.label} (${framework.label}) · file ext ${languageFileExt(language.id)}.`,
  }
}

/** Build a chip describing the live build-target snapshot. */
export function buildTargetChip(target: BuildTargetOption): WorkspaceChatAnnotation {
  return {
    id: `build-target:${target.id}`,
    label: `Target · ${target.label}`,
    description: target.description,
  }
}

/**
 * Naïve unified-diff line classifier — matches the Web / Mobile pages'
 * shape so shared snapshot tests would line up.  Empty / non-string
 * diffs return an empty list so callers can fall back to "no changes".
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

/**
 * Filter a terminal line list by the active stream-toggle set.  Pure —
 * `streams.size === 0` collapses to the empty array (the operator
 * disabled every stream; nothing matches), not the full list.  This is
 * deliberate: a "show nothing" state is a real UX option for an idle
 * operator who wants to silence the panel without leaving the page.
 */
export function filterTerminalLines(
  lines: readonly TerminalLine[],
  streams: ReadonlySet<TerminalStream>,
): TerminalLine[] {
  return lines.filter((l) => streams.has(l.stream))
}

// ─── Sample artifacts (V8 #1 stub — replaced once the agent wire lands) ──

const SAMPLE_CODE: SoftwareCodeArtifact = {
  id: "src/api/users.py",
  label: "src/api/users.py",
  source:
    `from fastapi import APIRouter, HTTPException\n` +
    `\n` +
    `router = APIRouter(prefix="/users", tags=["users"])\n` +
    `\n` +
    `@router.get("")\n` +
    `def list_users(limit: int = 50, cursor: str | None = None):\n` +
    `    return {"items": [], "cursor": None}\n`,
  diff: null,
}

const SAMPLE_TERMINAL_LINES: readonly TerminalLine[] = Object.freeze([
  {
    id: "term-1",
    ts: "2026-04-25T07:30:00.000Z",
    stream: "build",
    severity: "info",
    text: "$ docker buildx build --platform linux/amd64,linux/arm64 -t omnisight/sample:dev .",
  },
  {
    id: "term-2",
    ts: "2026-04-25T07:30:01.120Z",
    stream: "build",
    severity: "stdout",
    text: "[+] Building 12.3s (8/8) FINISHED",
  },
  {
    id: "term-3",
    ts: "2026-04-25T07:30:13.500Z",
    stream: "test",
    severity: "info",
    text: "$ pytest -q tests/",
  },
  {
    id: "term-4",
    ts: "2026-04-25T07:30:14.090Z",
    stream: "test",
    severity: "stdout",
    text: "..........                                                              [100%]",
  },
  {
    id: "term-5",
    ts: "2026-04-25T07:30:14.140Z",
    stream: "test",
    severity: "stdout",
    text: "10 passed in 0.42s",
  },
])

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

interface SoftwareProjectTreeProps {
  nodes: readonly ProjectTreeNode[]
  onSelectFile?: (node: ProjectTreeNode) => void
}

function SoftwareProjectTree({ nodes, onSelectFile }: SoftwareProjectTreeProps) {
  return (
    <ul data-testid="software-project-tree" className="flex flex-col gap-0.5 text-xs">
      {nodes.map((n) => (
        <SoftwareProjectTreeRow key={n.id} node={n} depth={0} onSelectFile={onSelectFile} />
      ))}
    </ul>
  )
}

interface SoftwareProjectTreeRowProps {
  node: ProjectTreeNode
  depth: number
  onSelectFile?: (node: ProjectTreeNode) => void
}

function SoftwareProjectTreeRow({ node, depth, onSelectFile }: SoftwareProjectTreeRowProps) {
  const [expanded, setExpanded] = React.useState<boolean>(depth === 0)
  if (node.kind === "dir") {
    return (
      <li>
        <button
          type="button"
          data-testid={`software-project-tree-dir-${node.id}`}
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
              <SoftwareProjectTreeRow
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
        data-testid={`software-project-tree-file-${node.id}`}
        onClick={() => onSelectFile?.(node)}
        className="flex w-full items-center gap-1.5 rounded-sm px-1.5 py-1 text-left text-muted-foreground hover:bg-accent hover:text-foreground"
        style={{ paddingLeft: `${depth * 12 + 18}px` }}
      >
        <span className="truncate">{node.name}</span>
      </button>
    </li>
  )
}

interface LanguageFrameworkSelectorProps {
  options: readonly LanguageOption[]
  language: SoftwareLanguage
  framework: FrameworkOption
  onLanguageChange: (id: SoftwareLanguage) => void
  onFrameworkChange: (id: string) => void
}

function LanguageFrameworkSelector({
  options,
  language,
  framework,
  onLanguageChange,
  onFrameworkChange,
}: LanguageFrameworkSelectorProps) {
  const activeLanguage = options.find((o) => o.id === language) ?? options[0]
  return (
    <div data-testid="software-language-framework-selector" className="flex flex-col gap-2 text-xs">
      <ul className="flex flex-col gap-1">
        {options.map((option) => {
          const isSelected = option.id === language
          return (
            <li key={option.id}>
              <button
                type="button"
                data-testid={`software-language-option-${option.id}`}
                data-selected={isSelected ? "true" : "false"}
                aria-pressed={isSelected}
                onClick={() => onLanguageChange(option.id)}
                className={cn(
                  "flex w-full items-start justify-between gap-2 rounded-md border border-border/60 px-2 py-1.5 text-left text-xs transition-colors",
                  isSelected
                    ? "border-primary/60 bg-primary/10 text-foreground"
                    : "bg-background/50 text-muted-foreground hover:bg-accent hover:text-foreground",
                )}
              >
                <span className="flex flex-col gap-0.5">
                  <span className="flex items-center gap-1.5 font-medium text-foreground">
                    <Code2 className="size-3.5" aria-hidden="true" />
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

      <div className="flex flex-col gap-1.5">
        <Label className="text-[11px] uppercase tracking-wider">Framework</Label>
        <Select
          data-testid="software-framework-select"
          value={framework.id}
          onValueChange={onFrameworkChange}
        >
          <SelectTrigger
            data-testid="software-framework-trigger"
            className="h-8 text-xs"
          >
            <SelectValue placeholder="Pick framework" />
          </SelectTrigger>
          <SelectContent>
            {activeLanguage.frameworks.map((f) => (
              <SelectItem
                key={f.id}
                value={f.id}
                data-testid={`software-framework-item-${f.id}`}
              >
                {f.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
    </div>
  )
}

interface BuildTargetSelectorProps {
  options: readonly BuildTargetOption[]
  value: BuildTarget
  onChange: (id: BuildTarget) => void
  onSendToAgent: () => void
}

function BuildTargetSelector({
  options,
  value,
  onChange,
  onSendToAgent,
}: BuildTargetSelectorProps) {
  if (options.length === 0) {
    return (
      <span
        data-testid="software-build-target-empty"
        className="text-[10px] uppercase tracking-wider text-muted-foreground"
      >
        No targets for framework
      </span>
    )
  }
  const effective = options.some((o) => o.id === value) ? value : options[0].id
  return (
    <div data-testid="software-build-target-selector" className="flex flex-col gap-2 text-xs">
      <Select
        data-testid="software-build-target-select"
        value={effective}
        onValueChange={(v) => onChange(v as BuildTarget)}
      >
        <SelectTrigger
          data-testid="software-build-target-trigger"
          className="h-8 text-xs"
        >
          <SelectValue placeholder="Pick build target" />
        </SelectTrigger>
        <SelectContent>
          {options.map((opt) => (
            <SelectItem
              key={opt.id}
              value={opt.id}
              data-testid={`software-build-target-item-${opt.id}`}
            >
              {opt.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>

      <Button
        type="button"
        data-testid="software-build-target-send"
        onClick={onSendToAgent}
        size="sm"
        variant="secondary"
        className="h-7 text-xs"
      >
        <Send className="mr-1 size-3" aria-hidden="true" />
        Send target to agent
      </Button>
    </div>
  )
}

interface CenterTabsProps {
  active: SoftwareCenterTab
  onChange: (tab: SoftwareCenterTab) => void
}

function CenterTabs({ active, onChange }: CenterTabsProps) {
  return (
    <Tabs
      value={active}
      onValueChange={(v) => onChange(v as SoftwareCenterTab)}
      data-testid="software-center-tabs"
    >
      <TabsList className="h-8">
        <TabsTrigger
          value="terminal"
          data-testid="software-center-tab-terminal"
          className="h-7 gap-1.5 px-2 text-xs"
        >
          <Terminal className="size-3.5" aria-hidden="true" />
          Terminal
        </TabsTrigger>
        <TabsTrigger
          value="openapi"
          data-testid="software-center-tab-openapi"
          className="h-7 gap-1.5 px-2 text-xs"
        >
          <FileCode className="size-3.5" aria-hidden="true" />
          OpenAPI
        </TabsTrigger>
      </TabsList>
    </Tabs>
  )
}

interface TerminalOutputViewerProps {
  lines: readonly TerminalLine[]
  enabledStreams: ReadonlySet<TerminalStream>
  onToggleStream: (stream: TerminalStream) => void
  autoScroll: boolean
  onAutoScrollChange: (next: boolean) => void
}

function TerminalOutputViewer({
  lines,
  enabledStreams,
  onToggleStream,
  autoScroll,
  onAutoScrollChange,
}: TerminalOutputViewerProps) {
  const visible = React.useMemo(
    () => filterTerminalLines(lines, enabledStreams),
    [lines, enabledStreams],
  )
  const bodyRef = React.useRef<HTMLDivElement | null>(null)

  // Auto-scroll on new lines if the operator hasn't disabled it.  We
  // intentionally key on `visible.length` so a re-filter doesn't yank
  // the scrollbar; only growth does.
  const lastSeenLengthRef = React.useRef<number>(visible.length)
  React.useEffect(() => {
    if (!autoScroll) {
      lastSeenLengthRef.current = visible.length
      return
    }
    if (visible.length > lastSeenLengthRef.current) {
      const node = bodyRef.current
      if (node) node.scrollTop = node.scrollHeight
    }
    lastSeenLengthRef.current = visible.length
  }, [autoScroll, visible.length])

  return (
    <article
      data-testid="software-terminal-viewer"
      className="flex h-full min-h-0 flex-col overflow-hidden rounded-md border border-border bg-background"
    >
      <header className="flex flex-wrap items-center justify-between gap-2 border-b border-border bg-muted/40 px-2 py-1">
        <div
          data-testid="software-terminal-stream-toggles"
          className="flex flex-wrap items-center gap-1"
        >
          {TERMINAL_STREAMS.map((stream) => {
            const enabled = enabledStreams.has(stream)
            return (
              <button
                key={stream}
                type="button"
                data-testid={`software-terminal-stream-toggle-${stream}`}
                data-enabled={enabled ? "true" : "false"}
                aria-pressed={enabled}
                onClick={() => onToggleStream(stream)}
                className={cn(
                  "rounded-md border px-1.5 py-0.5 text-[10px] uppercase tracking-wider transition-colors",
                  enabled
                    ? "border-primary/60 bg-primary/10 text-foreground"
                    : "border-border bg-background/50 text-muted-foreground hover:text-foreground",
                )}
              >
                {stream}
              </button>
            )
          })}
        </div>
        <label
          htmlFor="software-terminal-autoscroll"
          className="flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-muted-foreground"
        >
          Auto-scroll
          <Switch
            id="software-terminal-autoscroll"
            data-testid="software-terminal-autoscroll"
            checked={autoScroll}
            onCheckedChange={onAutoScrollChange}
          />
        </label>
      </header>
      <div
        ref={bodyRef}
        data-testid="software-terminal-body"
        className="min-h-0 flex-1 overflow-auto bg-black/85 p-2 font-mono text-[11px] leading-snug text-emerald-200"
      >
        {visible.length === 0 ? (
          <p
            data-testid="software-terminal-empty"
            className="px-1 py-0.5 text-muted-foreground"
          >
            No output for the selected streams.
          </p>
        ) : (
          visible.map((line) => (
            <div
              key={line.id}
              data-testid={`software-terminal-line-${line.id}`}
              data-stream={line.stream}
              data-severity={line.severity}
              className={cn(
                "flex gap-2 whitespace-pre-wrap break-words px-1 py-0.5",
                line.severity === "stderr" || line.severity === "error"
                  ? "text-rose-300"
                  : line.severity === "info"
                    ? "text-sky-300"
                    : "text-emerald-100",
              )}
            >
              <span className="shrink-0 text-[10px] uppercase tracking-wider text-muted-foreground">
                {line.stream}
              </span>
              <span className="shrink-0 text-[10px] text-muted-foreground">
                {line.ts.slice(11, 19)}
              </span>
              <span className="min-w-0 flex-1">{line.text}</span>
            </div>
          ))
        )}
      </div>
    </article>
  )
}

const METHOD_BADGE_COLOURS: Record<OpenApiEndpoint["method"], string> = {
  get: "bg-sky-500/15 text-sky-400 border-sky-500/30",
  post: "bg-emerald-500/15 text-emerald-400 border-emerald-500/30",
  put: "bg-amber-500/15 text-amber-400 border-amber-500/30",
  patch: "bg-violet-500/15 text-violet-400 border-violet-500/30",
  delete: "bg-rose-500/15 text-rose-400 border-rose-500/30",
  options: "bg-slate-500/15 text-slate-300 border-slate-500/30",
  head: "bg-slate-500/15 text-slate-300 border-slate-500/30",
  trace: "bg-slate-500/15 text-slate-300 border-slate-500/30",
}

interface OpenApiDocsViewerProps {
  spec: OpenApiSpec | null
  selectedEndpointIds: readonly string[]
  onToggleEndpoint: (endpoint: OpenApiEndpoint) => void
}

function OpenApiDocsViewer({
  spec,
  selectedEndpointIds,
  onToggleEndpoint,
}: OpenApiDocsViewerProps) {
  const endpoints = React.useMemo(() => flattenOpenApiSpec(spec), [spec])
  const selected = React.useMemo(() => new Set(selectedEndpointIds), [selectedEndpointIds])
  const [expandedId, setExpandedId] = React.useState<string | null>(() =>
    endpoints.length > 0 ? endpointId(endpoints[0]) : null,
  )

  if (!spec) {
    return (
      <article
        data-testid="software-openapi-viewer"
        className="flex h-full min-h-0 flex-col overflow-hidden rounded-md border border-border bg-background"
      >
        <div
          data-testid="software-openapi-empty"
          className="flex flex-1 items-center justify-center p-6 text-center text-xs text-muted-foreground"
        >
          No OpenAPI spec loaded yet — ask the agent to generate one.
        </div>
      </article>
    )
  }

  return (
    <article
      data-testid="software-openapi-viewer"
      className="flex h-full min-h-0 flex-col overflow-hidden rounded-md border border-border bg-background"
    >
      <header className="flex items-baseline gap-2 border-b border-border bg-muted/40 px-3 py-1.5 text-xs">
        <span data-testid="software-openapi-title" className="font-semibold text-foreground">
          {spec.info?.title ?? "OpenAPI"}
        </span>
        {spec.info?.version && (
          <Badge
            data-testid="software-openapi-version"
            variant="outline"
            className="h-4 px-1.5 text-[10px] font-mono"
          >
            v{spec.info.version}
          </Badge>
        )}
        {spec.openapi && (
          <span
            data-testid="software-openapi-spec-version"
            className="ml-auto text-[10px] uppercase tracking-wider text-muted-foreground"
          >
            OpenAPI {spec.openapi}
          </span>
        )}
      </header>
      <ul
        data-testid="software-openapi-endpoint-list"
        className="min-h-0 flex-1 divide-y divide-border overflow-auto"
      >
        {endpoints.length === 0 ? (
          <li
            data-testid="software-openapi-empty-list"
            className="px-3 py-4 text-center text-xs text-muted-foreground"
          >
            Spec has no paths to render.
          </li>
        ) : (
          endpoints.map((endpoint) => {
            const id = endpointId(endpoint)
            const isExpanded = expandedId === id
            const isSelected = selected.has(id)
            return (
              <li key={id} data-testid={`software-openapi-endpoint-${endpoint.method}-${endpoint.path}`}>
                <div className="flex items-center gap-2 px-2 py-1.5">
                  <button
                    type="button"
                    data-testid={`software-openapi-endpoint-toggle-${endpoint.method}-${endpoint.path}`}
                    aria-expanded={isExpanded}
                    onClick={() => setExpandedId(isExpanded ? null : id)}
                    className="flex flex-1 items-center gap-2 text-left"
                  >
                    <Badge
                      variant="outline"
                      className={cn(
                        "h-5 min-w-[3rem] justify-center px-1 font-mono text-[10px] uppercase",
                        METHOD_BADGE_COLOURS[endpoint.method],
                      )}
                    >
                      {endpoint.method}
                    </Badge>
                    <code className="truncate text-xs">{endpoint.path}</code>
                    {endpoint.operation.summary && (
                      <span className="ml-2 truncate text-[11px] text-muted-foreground">
                        {endpoint.operation.summary}
                      </span>
                    )}
                  </button>
                  <Button
                    type="button"
                    data-testid={`software-openapi-endpoint-attach-${endpoint.method}-${endpoint.path}`}
                    data-selected={isSelected ? "true" : "false"}
                    aria-pressed={isSelected}
                    onClick={() => onToggleEndpoint(endpoint)}
                    size="sm"
                    variant={isSelected ? "secondary" : "ghost"}
                    className="h-6 gap-1 px-2 text-[10px] uppercase"
                  >
                    {isSelected ? <Check className="size-3" aria-hidden="true" /> : "Attach"}
                  </Button>
                </div>
                {isExpanded && (
                  <div
                    data-testid={`software-openapi-endpoint-detail-${endpoint.method}-${endpoint.path}`}
                    className="flex flex-col gap-2 border-t border-border bg-muted/20 px-3 py-2 text-xs"
                  >
                    {endpoint.operation.description && (
                      <p className="text-muted-foreground">{endpoint.operation.description}</p>
                    )}
                    {endpoint.operation.tags && endpoint.operation.tags.length > 0 && (
                      <div className="flex flex-wrap gap-1">
                        {endpoint.operation.tags.map((tag) => (
                          <Badge
                            key={tag}
                            variant="outline"
                            className="h-4 px-1.5 text-[10px]"
                            data-testid={`software-openapi-endpoint-tag-${endpoint.method}-${endpoint.path}-${tag}`}
                          >
                            {tag}
                          </Badge>
                        ))}
                      </div>
                    )}
                    {endpoint.operation.parameters && endpoint.operation.parameters.length > 0 && (
                      <table className="w-full table-fixed text-left text-[11px]">
                        <thead>
                          <tr className="text-[10px] uppercase tracking-wider text-muted-foreground">
                            <th className="w-[28%] pb-1">Name</th>
                            <th className="w-[14%] pb-1">In</th>
                            <th className="w-[14%] pb-1">Type</th>
                            <th className="w-[10%] pb-1">Req?</th>
                            <th className="pb-1">Description</th>
                          </tr>
                        </thead>
                        <tbody>
                          {endpoint.operation.parameters.map((param) => (
                            <tr
                              key={`${param.in}:${param.name}`}
                              data-testid={`software-openapi-endpoint-param-${endpoint.method}-${endpoint.path}-${param.name}`}
                              className="border-t border-border/40"
                            >
                              <td className="py-1 font-mono">{param.name}</td>
                              <td className="py-1 text-muted-foreground">{param.in}</td>
                              <td className="py-1 font-mono text-muted-foreground">
                                {param.schema?.type ?? "any"}
                              </td>
                              <td className="py-1 text-muted-foreground">
                                {param.required ? "yes" : "no"}
                              </td>
                              <td className="py-1 text-muted-foreground">
                                {param.description ?? ""}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    )}
                    {!endpoint.operation.description &&
                      !endpoint.operation.parameters?.length &&
                      !endpoint.operation.tags?.length && (
                        <p className="text-muted-foreground">
                          No further details in the spec for this operation.
                        </p>
                      )}
                  </div>
                )}
              </li>
            )
          })
        )}
      </ul>
    </article>
  )
}

interface SoftwareCodeViewerProps {
  artifact: SoftwareCodeArtifact
  languageFileExt: string
  onCopy: (text: string) => Promise<void> | void
  copyStatus: "idle" | "copied" | "error"
}

function SoftwareCodeViewer({
  artifact,
  languageFileExt,
  onCopy,
  copyStatus,
}: SoftwareCodeViewerProps) {
  const diffLines = React.useMemo(() => classifyDiffLines(artifact.diff ?? null), [
    artifact.diff,
  ])
  return (
    <article
      data-testid="software-code-viewer"
      data-artifact-id={artifact.id}
      data-language-ext={languageFileExt}
      className="flex min-h-0 flex-col overflow-hidden rounded-md border border-border bg-background"
    >
      <header className="flex items-center justify-between gap-2 border-b border-border bg-muted/40 px-2 py-1">
        <div className="flex items-center gap-1.5 text-xs font-medium">
          <ExternalLink className="size-3.5 text-muted-foreground" aria-hidden="true" />
          <span data-testid="software-code-viewer-label" className="truncate">
            {shortenPath(artifact.label)}
          </span>
          <Badge
            data-testid="software-code-viewer-ext-badge"
            variant="outline"
            className="ml-1 h-4 px-1.5 text-[10px] font-mono"
          >
            {languageFileExt}
          </Badge>
          {diffLines.length > 0 && (
            <Badge
              data-testid="software-code-viewer-diff-badge"
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
          data-testid="software-code-viewer-copy"
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
      <div data-testid="software-code-viewer-body" className="min-h-0 flex-1 overflow-auto">
        {diffLines.length > 0 ? (
          <pre
            data-testid="software-code-viewer-diff"
            className="m-0 whitespace-pre overflow-x-auto p-2 font-mono text-[11px] leading-snug"
          >
            {diffLines.map((line, idx) => (
              <span
                key={idx}
                data-testid={`software-code-viewer-diff-line-${idx}`}
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
            data-testid="software-code-viewer-source"
            className="m-0 whitespace-pre overflow-x-auto p-2 font-mono text-[11px] leading-snug text-foreground"
          >
            {artifact.source}
          </pre>
        )}
      </div>
    </article>
  )
}

// ─── Page contents (read provider; mount surfaces) ────────────────────────

export interface SoftwareWorkspacePageContentsProps {
  /** Initial language — exposed so tests and storybook can pin it. */
  initialLanguage?: SoftwareLanguage
  /** Initial framework id (must belong to `initialLanguage`'s catalogue). */
  initialFrameworkId?: string
  /** Initial build target — honoured if framework allows it, else falls back. */
  initialBuildTarget?: BuildTarget
  /** Initial centre tab — `terminal` or `openapi`. */
  initialCenterTab?: SoftwareCenterTab
  /** Sample code artifact shown until the agent wire lands. */
  initialArtifact?: SoftwareCodeArtifact
  /** Project tree to render in the sidebar. */
  projectTree?: readonly ProjectTreeNode[]
  /** Initial terminal lines (live-stream replacement comes from SSE). */
  initialTerminalLines?: readonly TerminalLine[]
  /** Initial OpenAPI spec — set `null` to show the empty state. */
  initialOpenApiSpec?: OpenApiSpec | null
  /**
   * Inject a clipboard writer in tests (jsdom has no `navigator.clipboard`
   * by default).  Defaults to `navigator.clipboard.writeText`.
   */
  copyToClipboardImpl?: (text: string) => Promise<void>
  /**
   * Test seam: forward agent-bound submissions out of the page so tests
   * can assert composer payload shape without mocking the backend.
   * Carries the structured language / framework / build-target /
   * endpoint context alongside the base submission (same pattern as
   * the Web / Mobile pages).
   */
  onAgentSubmit?: (
    submission: WorkspaceChatSubmission & {
      language: SoftwareLanguage
      framework: FrameworkOption
      buildTarget: BuildTarget
      selectedEndpoints: OpenApiEndpoint[]
    },
  ) => void | Promise<void>
}

export function SoftwareWorkspacePageContents({
  initialLanguage = DEFAULT_LANGUAGE,
  initialFrameworkId,
  initialBuildTarget = DEFAULT_BUILD_TARGET,
  initialCenterTab = DEFAULT_CENTER_TAB,
  initialArtifact = SAMPLE_CODE,
  projectTree = DEFAULT_PROJECT_TREE,
  initialTerminalLines = SAMPLE_TERMINAL_LINES,
  initialOpenApiSpec = SAMPLE_OPENAPI_SPEC,
  copyToClipboardImpl,
  onAgentSubmit,
}: SoftwareWorkspacePageContentsProps) {
  const ctx = useWorkspaceContext()
  const [language, setLanguage] = React.useState<SoftwareLanguage>(initialLanguage)
  const [frameworkId, setFrameworkId] = React.useState<string>(
    initialFrameworkId ?? resolveLanguage(initialLanguage).frameworks[0].id,
  )
  const [buildTarget, setBuildTarget] = React.useState<BuildTarget>(initialBuildTarget)
  const [centerTab, setCenterTab] = React.useState<SoftwareCenterTab>(initialCenterTab)
  const [enabledStreams, setEnabledStreams] = React.useState<Set<TerminalStream>>(
    () => new Set<TerminalStream>(TERMINAL_STREAMS),
  )
  const [autoScroll, setAutoScroll] = React.useState<boolean>(true)
  const [openApiSpec] = React.useState<OpenApiSpec | null>(initialOpenApiSpec)
  const [selectedEndpointIds, setSelectedEndpointIds] = React.useState<string[]>([])
  const [buildTargetSnapshot, setBuildTargetSnapshot] =
    React.useState<BuildTarget | null>(null)
  const [chatLog, setChatLog] = React.useState<WorkspaceChatMessage[]>([])
  const [copyStatus, setCopyStatus] = React.useState<"idle" | "copied" | "error">("idle")

  // Resolve the active language + framework + target — pinned to current
  // state but funnelled through the helpers so a stale frameworkId
  // (e.g. "ts:fastify" right after a swap to Python) snaps to the
  // language's default.  Mirrors the Mobile page's `resolveActiveDevice`.
  const activeLanguage = React.useMemo(() => resolveLanguage(language), [language])
  const activeFramework = React.useMemo(
    () => resolveFramework(language, frameworkId),
    [language, frameworkId],
  )
  const activeTargets = React.useMemo(
    () => targetsForFramework(activeFramework),
    [activeFramework],
  )
  const activeBuildTarget = React.useMemo(
    () => resolveActiveBuildTarget(activeFramework, buildTarget),
    [activeFramework, buildTarget],
  )
  const activeBuildTargetOption = React.useMemo(
    () => BUILD_TARGET_OPTIONS.find((o) => o.id === activeBuildTarget) ?? BUILD_TARGET_OPTIONS[0],
    [activeBuildTarget],
  )

  // Keep frameworkId valid when language changes — if the current
  // framework doesn't belong to the new language's catalogue, snap to
  // the language's first entry.  Same useEffect pattern Mobile uses
  // for device-on-platform-change.
  React.useEffect(() => {
    setFrameworkId((current) => {
      const resolved = resolveFramework(language, current)
      return resolved.id
    })
  }, [language])

  // Keep buildTarget valid when framework changes (cascade from above).
  React.useEffect(() => {
    setBuildTarget((current) => resolveActiveBuildTarget(activeFramework, current))
  }, [activeFramework])

  const sendBuildTargetToAgent = React.useCallback(() => {
    setBuildTargetSnapshot(activeBuildTarget)
  }, [activeBuildTarget])

  const toggleStream = React.useCallback((stream: TerminalStream) => {
    setEnabledStreams((prev) => {
      const next = new Set(prev)
      if (next.has(stream)) next.delete(stream)
      else next.add(stream)
      return next
    })
  }, [])

  const toggleEndpointSelection = React.useCallback((endpoint: OpenApiEndpoint) => {
    const id = endpointId(endpoint)
    setSelectedEndpointIds((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id],
    )
  }, [])

  const endpoints = React.useMemo(() => flattenOpenApiSpec(openApiSpec), [openApiSpec])

  // Annotation chips, language chip, build-target snapshot chip, and
  // selected-endpoint chips collectively form the chat-composer's
  // available references.
  const languageChipValue = React.useMemo<WorkspaceChatAnnotation>(
    () => languageChip(activeLanguage, activeFramework),
    [activeLanguage, activeFramework],
  )

  const buildChips = React.useMemo<WorkspaceChatAnnotation[]>(() => {
    if (!buildTargetSnapshot) return []
    const opt = BUILD_TARGET_OPTIONS.find((o) => o.id === buildTargetSnapshot)
    return opt ? [buildTargetChip(opt)] : []
  }, [buildTargetSnapshot])

  const endpointChips = React.useMemo<WorkspaceChatAnnotation[]>(
    () =>
      selectedEndpointIds
        .map((id) => endpoints.find((e) => endpointId(e) === id))
        .filter((e): e is OpenApiEndpoint => Boolean(e))
        .map(endpointChip),
    [endpoints, selectedEndpointIds],
  )

  const allChips = React.useMemo<WorkspaceChatAnnotation[]>(
    () => [languageChipValue, ...endpointChips, ...buildChips],
    [languageChipValue, endpointChips, buildChips],
  )

  const handleSubmit = React.useCallback(
    async (submission: WorkspaceChatSubmission) => {
      const selectedEndpoints = endpoints.filter((e) =>
        submission.annotationIds.includes(endpointId(e)),
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
          language,
          framework: activeFramework,
          buildTarget: activeBuildTarget,
          selectedEndpoints,
        })
      } finally {
        // Build-target snapshot is one-shot per submit so the chip clears;
        // language + endpoints stay selected so the operator can iterate.
        setBuildTargetSnapshot(null)
      }
    },
    [activeBuildTarget, activeFramework, ctx, endpoints, language, onAgentSubmit],
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
    <div data-testid="software-workspace-sidebar" className="flex flex-col">
      <SidebarSection
        title="Project tree"
        icon={<Folder className="size-3.5" aria-hidden="true" />}
        testId="software-sidebar-section-tree"
      >
        <SoftwareProjectTree nodes={projectTree} />
      </SidebarSection>
      <SidebarSection
        title="Language"
        icon={<Layers className="size-3.5" aria-hidden="true" />}
        testId="software-sidebar-section-language"
      >
        <LanguageFrameworkSelector
          options={LANGUAGE_OPTIONS}
          language={language}
          framework={activeFramework}
          onLanguageChange={setLanguage}
          onFrameworkChange={setFrameworkId}
        />
      </SidebarSection>
      <SidebarSection
        title="Build target"
        icon={<Boxes className="size-3.5" aria-hidden="true" />}
        testId="software-sidebar-section-build-target"
      >
        <BuildTargetSelector
          options={activeTargets}
          value={activeBuildTarget}
          onChange={setBuildTarget}
          onSendToAgent={sendBuildTargetToAgent}
        />
      </SidebarSection>
    </div>
  )

  const preview = (
    <div data-testid="software-workspace-preview" className="flex h-full min-h-0 flex-col">
      <div className="flex h-9 shrink-0 items-center justify-between gap-2 border-b border-border bg-background/40 px-3">
        <CenterTabs active={centerTab} onChange={setCenterTab} />
        <span
          data-testid="software-center-target-readout"
          className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-wider text-muted-foreground"
        >
          <Package className="size-3.5" aria-hidden="true" />
          {activeBuildTargetOption.label}
        </span>
      </div>
      <div
        data-testid="software-center-pane"
        data-tab={centerTab}
        className="min-h-0 flex-1 p-2"
      >
        {centerTab === "terminal" ? (
          <TerminalOutputViewer
            lines={initialTerminalLines}
            enabledStreams={enabledStreams}
            onToggleStream={toggleStream}
            autoScroll={autoScroll}
            onAutoScrollChange={setAutoScroll}
          />
        ) : (
          <OpenApiDocsViewer
            spec={openApiSpec}
            selectedEndpointIds={selectedEndpointIds}
            onToggleEndpoint={toggleEndpointSelection}
          />
        )}
      </div>
    </div>
  )

  const codeChat = (
    <div data-testid="software-workspace-code-chat" className="flex h-full min-h-0 flex-col gap-2 p-2">
      <div className="min-h-0 flex-1">
        <SoftwareCodeViewer
          artifact={initialArtifact}
          languageFileExt={languageFileExt(language)}
          onCopy={copy}
          copyStatus={copyStatus}
        />
      </div>
      <Separator className="my-1" />
      <div className="min-h-[260px] flex-1">
        <WorkspaceChat
          workspaceType="software"
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
      type="software"
      sidebar={sidebar}
      preview={preview}
      codeChat={codeChat}
      sidebarTitle="Tree · Language · Target"
      previewTitle="Terminal · OpenAPI"
      codeChatTitle="Code & iteration"
    />
  )
}

// ─── Page entry (client component; provider-wrapped) ──────────────────────

export default function SoftwareWorkspacePage() {
  return (
    <PersistentWorkspaceProvider type="software">
      <SoftwareWorkspacePageContents />
      <WorkspaceOnboardingTour type="software" />
    </PersistentWorkspaceProvider>
  )
}
