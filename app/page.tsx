"use client"

import { useState, useCallback, useRef, useEffect } from "react"
import { NeuralGrid } from "@/components/omnisight/neural-grid"
import { NotificationCenter } from "@/components/omnisight/notification-center"
import { NPITimeline } from "@/components/omnisight/npi-timeline"
import { GlobalStatusHeader } from "@/components/omnisight/global-status-header"
import { SpecNode } from "@/components/omnisight/spec-node"
import { AgentMatrixWall, defaultAgents, type Agent, type AgentStatus } from "@/components/omnisight/agent-matrix-wall"
import { VitalsArtifactsPanel } from "@/components/omnisight/vitals-artifacts-panel"
import { DecisionDashboard } from "@/components/omnisight/decision-dashboard"
import { BudgetStrategyPanel } from "@/components/omnisight/budget-strategy-panel"
import { PipelineTimeline } from "@/components/omnisight/pipeline-timeline"
import { DecisionRulesEditor } from "@/components/omnisight/decision-rules-editor"
import { DagEditor } from "@/components/omnisight/dag-editor"
import { OpsSummaryPanel } from "@/components/omnisight/ops-summary-panel"
import { OrchestrationPanel } from "@/components/omnisight/orchestration-panel"
import { SpecTemplateEditor } from "@/components/omnisight/spec-template-editor"
import { RunHistoryPanel } from "@/components/omnisight/run-history-panel"
import { AuditPanel } from "@/components/omnisight/audit-panel"
import { PepLiveFeed } from "@/components/omnisight/pep-live-feed"
import { ChatOpsMirror } from "@/components/omnisight/chatops-mirror"
import type { ParsedSpec } from "@/lib/api"
import { UserMenu } from "@/components/omnisight/user-menu"
import { TenantSwitcher } from "@/components/omnisight/tenant-switcher"
import { useAuth } from "@/lib/auth-context"
import { useRouter } from "next/navigation"
import { ToastCenter } from "@/components/omnisight/toast-center"
import { FirstRunTour } from "@/components/omnisight/first-run-tour"
import { NewProjectWizard } from "@/components/omnisight/new-project-wizard"
import { CommandPalette } from "@/components/omnisight/command-palette"
import { ForecastPanel } from "@/components/omnisight/forecast-panel"
import { InvokeCore } from "@/components/omnisight/invoke-core"
import { IntegrationSettings, SettingsButton } from "@/components/omnisight/integration-settings"
import { HostDevicePanel } from "@/components/omnisight/host-device-panel"
import { SourceControlMatrix, type Repository } from "@/components/omnisight/source-control-matrix"
import { TaskBacklog } from "@/components/omnisight/task-backlog"
import { OrchestratorAI } from "@/components/omnisight/orchestrator-ai"
import { MobileNav, TabletNav, type PanelId } from "@/components/omnisight/mobile-nav"
import { useEngine } from "@/hooks/use-engine"
import * as api from "@/lib/api"

const agentTemplates: Record<string, Partial<Agent>> = {
  firmware: { type: "firmware", thoughtChain: "Initializing firmware build pipeline...", status: "booting" },
  software: { type: "software", thoughtChain: "Preparing software compilation...", status: "booting" },
  validator: { type: "validator", thoughtChain: "Awaiting validation tasks...", status: "idle" },
  reporter: { type: "reporter", thoughtChain: "Reporter node standing by...", status: "idle" },
  custom: { type: "custom", thoughtChain: "Custom agent initialized...", status: "idle" },
}

// Phase 50D: parse ?panel=…&decision=… so a URL can deep-link into a
// specific panel, and optionally focus a decision id in the dashboard.
const VALID_PANELS: ReadonlySet<PanelId> = new Set([
  "host", "spec", "agents", "orchestrator", "tasks", "source", "npi", "vitals",
  "decisions", "budget", "timeline", "rules", "forecast", "dag", "intent", "history", "audit",
  "pep", "chatops",
])

function readPanelFromUrl(): PanelId | null {
  if (typeof window === "undefined") return null
  const qs = new URLSearchParams(window.location.search)
  const panel = qs.get("panel")
  if (panel && (VALID_PANELS as Set<string>).has(panel)) return panel as PanelId
  if (qs.get("decision")) return "decisions"
  return null
}

export default function Home() {
  // Internet-exposure gate: redirect to /login if the cookie isn't
  // good. In auth_mode=open the backend whoami returns a synthetic
  // admin user, so this check is a no-op for dev — exactly what we
  // want for backwards compatibility with the single-user flow.
  const auth = useAuth()
  const router = useRouter()
  useEffect(() => {
    if (!auth.loading && !auth.user && auth.authMode !== "open") {
      const next = typeof window !== "undefined"
        ? window.location.pathname + window.location.search
        : "/"
      router.replace(`/login?next=${encodeURIComponent(next)}`)
    }
  }, [auth.loading, auth.user, auth.authMode, router])

  const engine = useEngine()
  const [syncCount, setSyncCount] = useState(0)
  // R2-#21: always hydrate with the same value as the server
  // (`orchestrator`). Apply the URL-derived panel in a mount effect so
  // SSR markup and the first client render never disagree — previously
  // a deep link rendered "orchestrator" on the server but "decisions"
  // on the client, triggering React hydration warnings.
  const [activePanel, setActivePanel] = useState<PanelId>("orchestrator")
  useEffect(() => {
    const p = readPanelFromUrl()
    if (p && p !== "orchestrator") setActivePanel(p)
  }, [])
  const [providerData, setProviderData] = useState<api.ProvidersResponse | null>(null)
  const [providerHealth, setProviderHealth] = useState<api.ProviderHealthResponse | null>(null)
  const [handoffs, setHandoffs] = useState<api.HandoffItem[]>([])
  const [showSettings, setShowSettings] = useState(false)
  const [showNotifications, setShowNotifications] = useState(false)

  // Fetch provider list + health on mount and periodically
  // Fix-C C2: surface fetch failures at debug level so a dead backend
  // isn't 100% invisible (was: silent `catch(() => {})`).
  const refetchProviders = useCallback(() => {
    api.getProviders().then(setProviderData).catch((e) => {
      console.debug("[providers] list fetch failed:", e)
    })
    api.getProviderHealth().then(setProviderHealth).catch((e) => {
      console.debug("[providers] health fetch failed:", e)
    })
  }, [])
  useEffect(() => {
    refetchProviders()
    const healthInterval = setInterval(() => {
      api.getProviderHealth().then(setProviderHealth).catch((e) => {
        console.debug("[providers] health poll failed:", e)
      })
    }, 10000)
    return () => clearInterval(healthInterval)
  }, [refetchProviders])
  // Sync provider data when switched from any source (Settings or Orchestrator)
  useEffect(() => {
    engine.setProviderSwitchCallback(refetchProviders)
    return () => engine.setProviderSwitchCallback(null)
  }, [refetchProviders, engine])  // engine is a stable singleton; listed for exhaustive-deps compliance

  // Phase 50D: keep ?panel=… in sync with activePanel and honour the
  // browser back/forward button. Preserves any ?decision=… query so a
  // deep link like `/?decision=dec-x&panel=timeline` survives navigation.
  useEffect(() => {
    if (typeof window === "undefined") return
    const params = new URLSearchParams(window.location.search)
    const currentParam = params.get("panel")
    if (currentParam === activePanel) return
    params.set("panel", activePanel)
    const next = `${window.location.pathname}?${params.toString()}`
    window.history.replaceState(null, "", next)
  }, [activePanel])

  useEffect(() => {
    if (typeof window === "undefined") return
    const onPop = () => {
      const p = new URLSearchParams(window.location.search).get("panel")
      if (p && (VALID_PANELS as Set<string>).has(p)) setActivePanel(p as PanelId)
    }
    window.addEventListener("popstate", onPop)
    return () => window.removeEventListener("popstate", onPop)
  }, [])

  // Programmatic panel navigation from nested components (e.g. DagEditor
  // jumping to the timeline after a successful submit). Using a custom
  // event keeps the child decoupled from the top-level state setter.
  useEffect(() => {
    if (typeof window === "undefined") return
    const onNav = (e: Event) => {
      const detail = (e as CustomEvent<{ panel?: string }>).detail
      const p = detail?.panel
      if (p && (VALID_PANELS as Set<string>).has(p)) setActivePanel(p as PanelId)
    }
    window.addEventListener("omnisight:navigate", onNav as EventListener)
    return () => window.removeEventListener("omnisight:navigate", onNav as EventListener)
  }, [])

  // Use engine state (backed by API when connected)
  const {
    agents, tasks, messages: orchestratorMessages,
    systemStatus, systemInfo, devices: sysDevices,
    spec, repos, logs, tokenUsage,
  } = engine

  // INVOKE ref: capture the current command input text
  const invokeCommandRef = useRef("")

  const handleInvoke = useCallback(async () => {
    setSyncCount(c => c + 1)
    // If there's text in the command input, send it as a priority command
    // Otherwise, perform context-aware singularity sync
    const cmd = invokeCommandRef.current.trim() || undefined
    await engine.invoke(cmd)
  }, [engine])

  const handleAddAgent = useCallback(async (type?: Agent["type"], _tools?: string[], subType?: string, aiModel?: string) => {
    const agentType = type || (() => {
      const types = Object.keys(agentTemplates)
      return types[Math.floor(Math.random() * types.length)] as Agent["type"]
    })()
    await engine.addAgent(agentType, undefined, subType, aiModel)
  }, [engine])

  const handleRemoveAgent = useCallback(async (id: string) => {
    await engine.removeAgent(id)
  }, [engine])

  const handleConfirmAgent = useCallback((id: string) => {
    engine.patchAgentLocal(id, {
      status: "success" as AgentStatus,
      requiresConfirmation: false,
      thoughtChain: "User confirmed. Operations approved and finalized.",
    })
  }, [engine])

  const handleRejectAgent = useCallback((id: string) => {
    engine.patchAgentLocal(id, {
      status: "error" as AgentStatus,
      requiresConfirmation: false,
      thoughtChain: "User rejected. Rolling back operations...",
    })
  }, [engine])

  const handleRetryAgent = useCallback((id: string) => {
    const agent = agents.find(a => a.id === id)
    engine.patchAgentLocal(id, {
      status: "running" as AgentStatus,
      progress: { current: 0, total: agent?.progress.total ?? 5 },
      thoughtChain: "Retrying operations from checkpoint...",
    })
  }, [engine, agents])

  // Emergency stop / resume
  const [isHalted, setIsHalted] = useState(false)
  const [haltedAgentStates, setHaltedAgentStates] = useState<Map<string, { status: AgentStatus; thoughtChain: string }>>(new Map())

  const handleEmergencyStop = useCallback(async () => {
    const statesToSave = new Map<string, { status: AgentStatus; thoughtChain: string }>()
    agents.forEach(agent => {
      if (agent.status === "running" || agent.status === "booting") {
        statesToSave.set(agent.id, { status: agent.status, thoughtChain: agent.thoughtChain })
      }
    })
    setHaltedAgentStates(statesToSave)
    setIsHalted(true)

    // Notify backend to halt any running INVOKE
    try { await api.haltInvoke() } catch { /* backend may not be reachable */ }

    engine.setAgents(prev => prev.map(agent => ({
      ...agent,
      status: agent.status === "running" || agent.status === "booting" ? "warning" as AgentStatus : agent.status,
      thoughtChain: agent.status === "running" || agent.status === "booting"
        ? "HALTED - Operations suspended. Click RESUME to continue."
        : agent.thoughtChain,
    })))
  }, [agents, engine])

  const handleResume = useCallback(async () => {
    // Notify backend to resume
    try { await api.resumeInvoke() } catch { /* backend may not be reachable */ }

    engine.setAgents(prev => prev.map(agent => {
      const savedState = haltedAgentStates.get(agent.id)
      if (savedState && agent.status === "warning") {
        return { ...agent, status: savedState.status, thoughtChain: `Resuming: ${savedState.thoughtChain}` }
      }
      return agent
    }))
    setHaltedAgentStates(new Map())
    setIsHalted(false)
  // eslint-disable-next-line react-hooks/exhaustive-deps -- haltedAgentStates is read inside setAgents callback
  }, [engine])

  const hasRunningAgents = agents.some(a => a.status === "running" || a.status === "booting")

  // Source Control handlers
  const handleTether = useCallback((repoId: string, agentId: string) => {
    engine.patchAgentLocal(agentId, { thoughtChain: `Workspace tethered: ${repoId}. Running git clone...` })
  }, [engine])

  const handleDetether = useCallback((repoId: string) => {
    engine.setAgents(prev => prev.map(agent => ({
      ...agent,
      thoughtChain: agent.thoughtChain.includes(repoId) ? "Workspace detethered. Standing by..." : agent.thoughtChain,
    })))
  }, [engine])

  const handleCreateRepo = useCallback((name: string, targetAgentId?: string) => {
    if (targetAgentId) {
      engine.patchAgentLocal(targetAgentId, {
        status: "running" as AgentStatus,
        thoughtChain: `Genesis initialized: ${name}. Generating project structure...`,
      })
    }
  }, [engine])

  const handleAddRepo = useCallback((repo: Repository) => {
    console.log("[Engine] Repository added:", repo.name, repo.url)
  }, [])

  const handleRemoveRepo = useCallback((repoId: string) => {
    console.log("[Engine] Repository removed:", repoId)
  }, [])

  // Task handlers — delegates to engine (API-backed)
  const handleAssignTask = useCallback(async (taskId: string, agentId: string) => {
    await engine.assignTask(taskId, agentId)
  }, [engine])

  const handleCreateAgentForTask = useCallback(async (type: Agent["type"], taskId?: string) => {
    const agent = await engine.addAgent(type)
    if (taskId && agent) {
      await engine.assignTask(taskId, agent.id)
    }
  }, [engine])

  const handleForceAssign = useCallback(async (taskId: string, agentId: string) => {
    await engine.forceAssign(taskId, agentId)
  }, [engine])

  const handleCompleteTask = useCallback(async (taskId: string) => {
    await engine.completeTask(taskId)
  }, [engine])

  const handleAddTask = useCallback(async (title: string, priority: string) => {
    try {
      await api.createTask({ title, priority })
      engine.refresh()
    } catch (e) {
      console.error("Failed to create task:", e)
    }
  }, [engine])

  const handleUpdateAgentFromTask = useCallback((agentId: string, status: AgentStatus, thoughtChain?: string) => {
    engine.patchAgentLocal(agentId, { status, ...(thoughtChain ? { thoughtChain } : {}) })
  }, [engine])

  // Materialization handlers
  // Materialization handlers removed — all agent creation goes through
  // handleAddAgent → engine.addAgent() → backend API for consistency.

  // Command handler — sends to backend via engine
  const handleCommand = useCallback(async (command: string) => {
    const cmd = command.toLowerCase().trim()

    // Local-only commands that don't need the backend
    if (cmd === "clear" || cmd === "reset") {
      engine.setAgents([])
      return
    }
    if (cmd === "restore" || cmd === "default") {
      engine.setAgents(defaultAgents)
      return
    }

    // Send everything else to the backend orchestrator
    await engine.sendCommand(command)
  }, [engine])

  const handleSpecChange = useCallback(async (path: string[], newValue: string | number | boolean) => {
    try {
      await api.updateSpec(path, newValue)
      engine.refresh()
    } catch (e) {
      console.error("[Engine] Spec update failed:", e)
    }
  }, [engine])

  // Phase-3 P5 root-cause gate (2026-04-20): DO NOT RENDER the dashboard
  // body — which mounts 7 panels that each fire their own mount fetch
  // via useEffect — until auth is resolved and the operator is
  // confirmed logged in. Without this gate, the sequence on a private
  // window / fresh session is:
  //
  //   1. Browser lands on `/?panel=orchestrator`
  //   2. Home mounts → all 7 panels mount → 14 parallel XHRs fire
  //   3. None of the XHRs carry a session cookie yet (not logged in)
  //   4. All 14 return 401
  //   5. lib/api.ts::_handleTerminalError on each 401 calls
  //      window.location.assign("/login?next=...") → 14 parallel
  //      full-page navigations (race, last wins) → browser flails
  //   6. The useEffect at line 75–82 would redirect cleanly on its own,
  //      but it runs AFTER the render that mounted the panels, so the
  //      401 cascade has already fired by the time it runs
  //
  // The gate below short-circuits step 2 before the panels ever mount.
  // The parallel useEffect at the top still handles the actual
  // router.replace to /login — this just ensures we don't mount the
  // children while waiting for it to run. In auth_mode=open the
  // backend whoami returns a synthetic admin so this is a no-op for
  // dev/single-user. Once the operator logs in, Home re-renders with
  // auth.user populated, panels mount once, stable state.
  if (auth.loading) {
    return (
      <div className="relative min-h-screen flex items-center justify-center">
        <NeuralGrid />
        <div className="relative z-10 text-sm text-[var(--neural-cyan,#67e8f9)] font-mono">
          Resolving session…
        </div>
      </div>
    )
  }
  if (!auth.user && auth.authMode !== "open") {
    // The redirect useEffect above will navigate; render a minimal
    // placeholder (NOT the 7-panel dashboard) so no child mounts its
    // fetch.
    return (
      <div className="relative min-h-screen flex items-center justify-center">
        <NeuralGrid />
        <div className="relative z-10 text-sm text-[var(--neural-cyan,#67e8f9)] font-mono">
          Redirecting to login…
        </div>
      </div>
    )
  }

  // Pre-map artifacts to avoid duplicating the mapping in renderPanel and desktop layout
  const mappedArtifacts = engine.artifacts.length > 0 ? engine.artifacts.map(a => ({
    id: a.id,
    name: a.name,
    type: a.type as "pdf" | "markdown" | "json" | "log" | "html" | "binary" | "firmware" | "kernel_module" | "sdk" | "model" | "archive",
    timestamp: a.created_at.includes("T") ? a.created_at.split("T")[1]?.slice(0, 8) || a.created_at : a.created_at,
    size: a.size > 1024 ? `${(a.size / 1024).toFixed(1)} KB` : `${a.size} B`,
  })) : undefined

  // Render panel based on active selection (for mobile/tablet)
  const renderPanel = (panelId: PanelId) => {
    switch (panelId) {
      case "host":
        return <HostDevicePanel
          hostInfo={systemInfo ? {
            hostname: systemInfo.hostname,
            os: systemInfo.os,
            kernel: systemInfo.kernel,
            arch: systemInfo.arch,
            cpuModel: systemInfo.cpu_model,
            cpuCores: systemInfo.cpu_cores,
            cpuUsage: systemInfo.cpu_usage,
            memoryTotal: systemInfo.memory_total,
            memoryUsed: systemInfo.memory_used,
            uptime: systemInfo.uptime,
          } : undefined}
          devices={sysDevices.length > 0 ? sysDevices.map(d => ({
            id: d.id,
            name: d.name,
            type: d.type,
            status: d.status,
            vendorId: d.vendorId,
            productId: d.productId,
            speed: d.speed ?? undefined,
            mountPoint: d.mountPoint,
            v4l2_device: d.v4l2_device,
            deploy_target_ip: d.deploy_target_ip,
            deploy_method: d.deploy_method,
            reachable: d.reachable,
          })) : undefined}
        />
      case "spec":
        return <SpecNode spec={spec.length > 0 ? (spec as never) : undefined} onSpecChange={handleSpecChange} />
      case "agents":
        return (
          <AgentMatrixWall
            agents={agents}
            onAddAgent={handleAddAgent}
            onRemoveAgent={handleRemoveAgent}
            onConfirmAgent={handleConfirmAgent}
            onRejectAgent={handleRejectAgent}
            onRetryAgent={handleRetryAgent}
          />
        )
      case "orchestrator":
        return (
          <OrchestratorAI
            agents={agents}
            tasks={tasks}
            onAssignTask={handleAssignTask}
            onSpawnAgent={handleCreateAgentForTask}
            onForceAssign={handleForceAssign}
            onUpdateAgentStatus={handleUpdateAgentFromTask}
            onCompleteTask={handleCompleteTask}
            externalMessages={orchestratorMessages}
            onSendCommand={handleCommand}
            tokenUsage={tokenUsage.length > 0 ? tokenUsage.map(t => ({
              model: t.model as string,
              inputTokens: t.input_tokens,
              outputTokens: t.output_tokens,
              totalTokens: t.total_tokens,
              cost: t.cost,
              requestCount: t.request_count,
              avgLatency: t.avg_latency,
              lastUsed: t.last_used,
            })) : undefined}
            tokenBudget={engine.tokenBudget}
            onResetFreeze={async () => { await api.resetTokenFreeze(); engine.refresh() }}
            onUpdateBudget={async (updates) => { await api.updateTokenBudget(updates as Record<string, number>); engine.refresh() }}
            onRefresh={() => engine.refresh()}
            compressionStats={engine.compressionStats}
            activeProvider={providerData?.active_provider}
            activeModel={providerData?.active_model}
            providers={providerData?.providers}
            providerHealth={providerHealth}
            onSwitchProvider={async (p, m) => { try { await api.switchProvider(p, m); setProviderData(await api.getProviders()); setProviderHealth(await api.getProviderHealth()) } catch (e) { console.error("Switch provider failed:", e) } }}
            onUpdateFallbackChain={async (chain) => { try { await api.updateFallbackChain(chain); setProviderHealth(await api.getProviderHealth()) } catch (e) { console.error("Update chain failed:", e) } }}
          />
        )
      case "tasks":
        return (
          <TaskBacklog
            agents={agents}
            tasks={tasks}
            onAssignTask={handleAssignTask}
            onCreateAgent={handleCreateAgentForTask}
            onUpdateAgentStatus={handleUpdateAgentFromTask}
            onAddTask={handleAddTask}
          />
        )
      case "source":
        return (
          <SourceControlMatrix
            agents={agents}
            repositories={repos.length > 0 ? repos.map(r => ({
              id: r.id, name: r.name, url: r.url, branch: r.branch,
              status: r.status, lastCommit: r.lastCommit,
              lastCommitTime: r.lastCommitTime, tetheredAgentId: r.tetheredAgentId ?? undefined,
            })) : undefined}
            onTether={handleTether}
            onDetether={handleDetether}
            onCreateRepo={handleCreateRepo}
            onAddRepo={handleAddRepo}
            onRemoveRepo={handleRemoveRepo}
          />
        )
      case "npi":
        return (
          <NPITimeline
            data={engine.npiData}
            onBusinessModelChange={async (model) => {
              try {
                const updated = await api.updateNPIState({ business_model: model })
                engine.setNpiData(updated)
              } catch { /* ignore – NPI update is best-effort */ }
            }}
            onMilestoneStatusChange={async (msId, status) => {
              try {
                await api.updateNPIMilestone(msId, status)
                const updated = await api.getNPIState()
                engine.setNpiData(updated)
              } catch { /* ignore – milestone update is best-effort */ }
            }}
          />
        )
      case "vitals":
        return <VitalsArtifactsPanel
              logs={logs.length > 0 ? logs : undefined}
              artifacts={mappedArtifacts}
              simulations={engine.simulations}
              onTriggerSimulation={async (track, module, mock) => {
                try { await api.triggerSimulation({ track, module, mock }) } catch (e) { console.error("Trigger simulation failed:", e) }
              }}
            />
      case "decisions":
        return <DecisionDashboard />
      case "budget":
        return <BudgetStrategyPanel />
      case "timeline":
        return <PipelineTimeline />
      case "rules":
        return <DecisionRulesEditor />
      case "forecast":
        return <ForecastPanel />
      case "dag":
        return <DagEditor />
      case "history":
        return <RunHistoryPanel />
      case "audit":
        return <AuditPanel />
      case "pep":
        return <PepLiveFeed />
      case "chatops":
        return <ChatOpsMirror />
      case "intent":
        return (
          <SpecTemplateEditor
            onSpecReady={(spec: ParsedSpec) => {
              // Hand off to DagEditor: navigate to the DAG panel and
              // seed it with a template best matching the parsed spec.
              // DagEditor listens for `omnisight:dag-seed-from-spec`
              // on window and pre-fills its JSON text accordingly.
              if (typeof window === "undefined") return
              window.dispatchEvent(
                new CustomEvent("omnisight:dag-seed-from-spec", {
                  detail: { spec },
                }),
              )
              window.dispatchEvent(
                new CustomEvent("omnisight:navigate", {
                  detail: { panel: "dag" },
                }),
              )
            }}
          />
        )
      default:
        return null
    }
  }

  return (
    <div className="relative min-h-screen flex flex-col overflow-hidden">
      {/* Neural Grid Background */}
      <NeuralGrid />

      {/* Phase 50C: overlay toasts for risky/destructive decisions. */}
      <ToastCenter />
      <FirstRunTour />
      <NewProjectWizard />
      <CommandPalette
        onNavigatePanel={(id) => {
          if ((VALID_PANELS as Set<string>).has(id)) setActivePanel(id as PanelId)
        }}
      />

      {/* Main Content */}
      <div className="relative z-10 flex flex-col h-screen">
        {/* Global Status Header */}
        <GlobalStatusHeader
          finished={systemStatus?.tasks_completed ?? syncCount}
          total={systemStatus?.tasks_total ?? tasks.length}
          inProgress={systemStatus?.agents_running ?? agents.filter(a => a.status === "running").length}
          wslStatus={systemStatus?.wsl_status === "OK" ? "OK" : "OFFLINE"}
          usbStatus={systemStatus?.usb_status ?? "Detecting..."}
          onEmergencyStop={handleEmergencyStop}
          onResume={handleResume}
          isHalted={isHalted}
          hasRunningAgents={hasRunningAgents || isHalted}
          unreadNotifications={engine.unreadCount}
          onToggleNotifications={() => setShowNotifications(prev => !prev)}
          settingsButton={
            <span className="inline-flex items-center gap-1">
              <TenantSwitcher />
              <UserMenu />
              <SettingsButton onClick={() => setShowSettings(true)} />
            </span>
          }
        />
        <IntegrationSettings open={showSettings} onClose={() => setShowSettings(false)} />

        {/* ===== MOBILE LAYOUT (< 768px) ===== */}
        <main className="flex-1 flex flex-col md:hidden min-h-0 pb-24">
          <div className="flex-1 p-3 overflow-y-auto overflow-x-hidden">
            {renderPanel(activePanel)}
          </div>
        </main>

        {/* ===== TABLET LAYOUT (768px - 1023px) ===== */}
        <main className="hidden md:flex lg:hidden flex-1 min-h-0">
          <TabletNav activePanel={activePanel} onPanelChange={setActivePanel} />
          <div className="flex-1 p-3 overflow-y-auto overflow-x-hidden">
            {renderPanel(activePanel)}
          </div>
        </main>

        {/* ===== DESKTOP LAYOUT (>= 1024px) ===== */}
        <main className="hidden lg:grid flex-1 grid-cols-[minmax(140px,180px)_minmax(140px,180px)_1fr_minmax(200px,240px)_minmax(140px,180px)_minmax(140px,180px)_minmax(160px,200px)_minmax(300px,360px)] gap-2 p-3 min-h-0 overflow-x-auto">
          {/* Far Left: Host & Devices */}
          <aside className="min-h-0 overflow-hidden">
            <HostDevicePanel
              hostInfo={systemInfo ? {
                hostname: systemInfo.hostname,
                os: systemInfo.os,
                kernel: systemInfo.kernel,
                arch: systemInfo.arch,
                cpuModel: systemInfo.cpu_model,
                cpuCores: systemInfo.cpu_cores,
                cpuUsage: systemInfo.cpu_usage,
                memoryTotal: systemInfo.memory_total,
                memoryUsed: systemInfo.memory_used,
                uptime: systemInfo.uptime,
              } : undefined}
              devices={sysDevices.length > 0 ? sysDevices.map(d => ({
                id: d.id,
                name: d.name,
                type: d.type,
                status: d.status,
                vendorId: d.vendorId,
                productId: d.productId,
                speed: d.speed ?? undefined,
                mountPoint: d.mountPoint,
                v4l2_device: d.v4l2_device,
                deploy_target_ip: d.deploy_target_ip,
                deploy_method: d.deploy_method,
                reachable: d.reachable,
              })) : undefined}
            />
          </aside>

          {/* Left: SPEC Node */}
          <aside className="min-h-0">
            <SpecNode spec={spec.length > 0 ? (spec as never) : undefined} onSpecChange={handleSpecChange} />
          </aside>

          {/* Center: Agent Matrix Wall */}
          <section className="min-h-0 overflow-hidden">
            <AgentMatrixWall
              agents={agents}
              onAddAgent={handleAddAgent}
              onRemoveAgent={handleRemoveAgent}
              onConfirmAgent={handleConfirmAgent}
              onRejectAgent={handleRejectAgent}
              onRetryAgent={handleRetryAgent}
            />
          </section>

          {/* Orchestrator AI - Central Coordinator & Command Hub */}
          <aside className="min-h-0 overflow-y-auto overflow-x-hidden">
            <OrchestratorAI
              agents={agents}
              tasks={tasks}
              onAssignTask={handleAssignTask}
              onSpawnAgent={handleCreateAgentForTask}
              onForceAssign={handleForceAssign}
              onUpdateAgentStatus={handleUpdateAgentFromTask}
              externalMessages={orchestratorMessages}
              onSendCommand={handleCommand}
              onCompleteTask={handleCompleteTask}
              tokenUsage={tokenUsage.length > 0 ? tokenUsage.map(t => ({
                model: t.model as string,
                inputTokens: t.input_tokens,
                outputTokens: t.output_tokens,
                totalTokens: t.total_tokens,
                cost: t.cost,
                requestCount: t.request_count,
                avgLatency: t.avg_latency,
                lastUsed: t.last_used,
              })) : undefined}
              tokenBudget={engine.tokenBudget}
              onResetFreeze={async () => { await api.resetTokenFreeze(); engine.refresh() }}
              onUpdateBudget={async (updates) => { await api.updateTokenBudget(updates as Record<string, number>); engine.refresh() }}
              onRefresh={() => engine.refresh()}
            compressionStats={engine.compressionStats}
              activeProvider={providerData?.active_provider}
              activeModel={providerData?.active_model}
              providers={providerData?.providers}
              providerHealth={providerHealth}
              onSwitchProvider={async (p, m) => { try { await api.switchProvider(p, m); setProviderData(await api.getProviders()); setProviderHealth(await api.getProviderHealth()) } catch (e) { console.error("Switch provider failed:", e) } }}
              onUpdateFallbackChain={async (chain) => { try { await api.updateFallbackChain(chain); setProviderHealth(await api.getProviderHealth()) } catch (e) { console.error("Update chain failed:", e) } }}
            handoffs={handoffs}
            onLoadHandoffs={async () => { try { setHandoffs(await api.getRecentHandoffs()) } catch { /* ignore – handoff load is best-effort */ } }}
            />
          </aside>

          {/* Task Backlog */}
          <aside className="min-h-0 overflow-y-auto overflow-x-hidden">
            <TaskBacklog
              agents={agents}
              tasks={tasks}
              onAssignTask={handleAssignTask}
              onCreateAgent={handleCreateAgentForTask}
              onUpdateAgentStatus={handleUpdateAgentFromTask}
              onAddTask={handleAddTask}
            />
          </aside>

          {/* Source Control Matrix */}
          <aside className="min-h-0 overflow-hidden">
            <SourceControlMatrix
              agents={agents}
              onTether={handleTether}
              onDetether={handleDetether}
              onCreateRepo={handleCreateRepo}
              onAddRepo={handleAddRepo}
              onRemoveRepo={handleRemoveRepo}
            />
          </aside>

          {/* NPI Lifecycle Timeline */}
          <aside className="min-h-0 overflow-y-auto overflow-x-hidden">
            <NPITimeline
              data={engine.npiData}
              onBusinessModelChange={async (model) => {
                try {
                  const updated = await api.updateNPIState({ business_model: model })
                  engine.setNpiData(updated)
                } catch { /* ignore – NPI update is best-effort */ }
              }}
              onMilestoneStatusChange={async (msId, status) => {
                try {
                  await api.updateNPIMilestone(msId, status)
                  const updated = await api.getNPIState()
                  engine.setNpiData(updated)
                } catch { /* ignore – milestone update is best-effort */ }
              }}
            />
          </aside>

          {/* Far Right: Ops + Decision Engine + Vitals & Artifacts.
              OpsSummary leads — it's the "is anything on fire?" glance. */}
          <aside className="min-h-0 min-w-0 overflow-y-auto overflow-x-hidden space-y-3">
            <OpsSummaryPanel />
            <OrchestrationPanel />
            <PipelineTimeline />
            <DecisionDashboard />
            <BudgetStrategyPanel />
            <DecisionRulesEditor />
            <VitalsArtifactsPanel
              logs={logs.length > 0 ? logs : undefined}
              artifacts={mappedArtifacts}
              simulations={engine.simulations}
              onTriggerSimulation={async (track, module, mock) => {
                try { await api.triggerSimulation({ track, module, mock }) } catch (e) { console.error("Trigger simulation failed:", e) }
              }}
            />
          </aside>
        </main>

        {/* Bottom: Invoke Core - Desktop */}
        <footer className="relative z-20 hidden lg:block">
          <InvokeCore onInvoke={handleInvoke} onCommand={handleCommand} onCommandChange={(v) => { invokeCommandRef.current = v }} />
        </footer>

        {/* Bottom: Invoke Core - Mobile/Tablet (above navigation) */}
        <footer className="relative z-20 lg:hidden mb-20 md:mb-0">
          <InvokeCore onInvoke={handleInvoke} onCommand={handleCommand} onCommandChange={(v) => { invokeCommandRef.current = v }} />
        </footer>
      </div>

      {/* Mobile Bottom Navigation */}
      <MobileNav activePanel={activePanel} onPanelChange={setActivePanel} />

      {/* Notification Center Panel */}
      <NotificationCenter
        open={showNotifications}
        onClose={() => setShowNotifications(false)}
        notifications={engine.notifications}
        onMarkRead={async (id) => {
          await api.markNotificationRead(id)
          engine.setUnreadCount(prev => Math.max(0, prev - 1))
        }}
      />
    </div>
  )
}
