"use client"

import { useState, useCallback } from "react"
import { NeuralGrid } from "@/components/omnisight/neural-grid"
import { GlobalStatusHeader } from "@/components/omnisight/global-status-header"
import { SpecNode } from "@/components/omnisight/spec-node"
import { AgentMatrixWall, defaultAgents, type Agent, type AgentStatus } from "@/components/omnisight/agent-matrix-wall"
import { VitalsArtifactsPanel } from "@/components/omnisight/vitals-artifacts-panel"
import { InvokeCore } from "@/components/omnisight/invoke-core"
import { HostDevicePanel } from "@/components/omnisight/host-device-panel"
import { SourceControlMatrix, type Repository } from "@/components/omnisight/source-control-matrix"
import { TaskBacklog, type Task } from "@/components/omnisight/task-backlog"
import { OrchestratorAI, type OrchestratorMessage } from "@/components/omnisight/orchestrator-ai"
import { MobileNav, TabletNav, type PanelId } from "@/components/omnisight/mobile-nav"

const agentTemplates: Record<string, Partial<Agent>> = {
  firmware: { type: "firmware", thoughtChain: "Initializing firmware build pipeline...", status: "booting" },
  software: { type: "software", thoughtChain: "Preparing software compilation...", status: "booting" },
  validator: { type: "validator", thoughtChain: "Awaiting validation tasks...", status: "idle" },
  reporter: { type: "reporter", thoughtChain: "Reporter node standing by...", status: "idle" },
  custom: { type: "custom", thoughtChain: "Custom agent initialized...", status: "idle" },
}

// Default tasks for orchestrator
const defaultTasks: Task[] = [
  {
    id: "task-1",
    title: "Build IMX335 camera driver",
    description: "Compile and test firmware for Sony IMX335 sensor",
    priority: "high",
    status: "backlog",
    createdAt: new Date().toISOString(),
    suggestedAgentType: "firmware"
  },
  {
    id: "task-2",
    title: "Run validation suite",
    description: "Execute full test coverage for ISP pipeline",
    priority: "medium",
    status: "backlog",
    createdAt: new Date().toISOString(),
    suggestedAgentType: "validator"
  },
  {
    id: "task-3",
    title: "Generate compliance report",
    description: "Create FCC/CE certification documentation",
    priority: "low",
    status: "backlog",
    createdAt: new Date().toISOString(),
    suggestedAgentType: "reporter"
  }
]

// Helper to format time consistently
function formatTime(): string {
  const date = new Date()
  const hours = date.getHours().toString().padStart(2, "0")
  const minutes = date.getMinutes().toString().padStart(2, "0")
  const seconds = date.getSeconds().toString().padStart(2, "0")
  return `${hours}:${minutes}:${seconds}`
}

export default function Home() {
  const [syncCount, setSyncCount] = useState(0)
  const [agents, setAgents] = useState<Agent[]>(defaultAgents)
  const [tasks, setTasks] = useState<Task[]>(defaultTasks)
  const [activePanel, setActivePanel] = useState<PanelId>("orchestrator")
  const [orchestratorMessages, setOrchestratorMessages] = useState<OrchestratorMessage[]>([])

  const handleInvoke = () => {
    setSyncCount(c => c + 1)
    // Simulate some agents starting work
    setAgents(prev => prev.map(agent => 
      agent.status === "idle" ? { ...agent, status: "running" as AgentStatus, thoughtChain: "Task invoked, processing..." } : agent
    ))
  }

  const handleAddAgent = useCallback(() => {
    const types = Object.keys(agentTemplates)
    const randomType = types[Math.floor(Math.random() * types.length)]
    const template = agentTemplates[randomType]
    const id = `agent-${Date.now()}`
    const newAgent: Agent = {
      id,
      name: `${randomType.toUpperCase()}_AGENT_${Math.floor(Math.random() * 100).toString().padStart(2, "0")}`,
      type: template.type as Agent["type"],
      status: template.status as AgentStatus,
      progress: { current: 0, total: Math.floor(Math.random() * 6) + 4 },
      thoughtChain: template.thoughtChain || "Initializing...",
    }
    setAgents(prev => [...prev, newAgent])
  }, [])

  const handleRemoveAgent = useCallback((id: string) => {
    setAgents(prev => prev.filter(agent => agent.id !== id))
  }, [])

  const handleConfirmAgent = useCallback((id: string) => {
    setAgents(prev => prev.map(agent => 
      agent.id === id 
        ? { 
            ...agent, 
            status: "success" as AgentStatus, 
            requiresConfirmation: false,
            thoughtChain: "User confirmed. Operations approved and finalized."
          } 
        : agent
    ))
  }, [])

  const handleRejectAgent = useCallback((id: string) => {
    setAgents(prev => prev.map(agent => 
      agent.id === id 
        ? { 
            ...agent, 
            status: "error" as AgentStatus, 
            requiresConfirmation: false,
            thoughtChain: "User rejected. Rolling back operations..."
          } 
        : agent
    ))
  }, [])

  const handleRetryAgent = useCallback((id: string) => {
    setAgents(prev => prev.map(agent => 
      agent.id === id 
        ? { 
            ...agent, 
            status: "running" as AgentStatus,
            progress: { current: 0, total: agent.progress.total },
            thoughtChain: "Retrying operations from checkpoint..."
          } 
        : agent
    ))
  }, [])

  const [isHalted, setIsHalted] = useState(false)
  const [haltedAgentStates, setHaltedAgentStates] = useState<Map<string, { status: AgentStatus; thoughtChain: string }>>(new Map())

  const handleEmergencyStop = useCallback(() => {
    // Store the current states of running agents for potential resume
    const statesToSave = new Map<string, { status: AgentStatus; thoughtChain: string }>()
    agents.forEach(agent => {
      if (agent.status === "running" || agent.status === "booting") {
        statesToSave.set(agent.id, { status: agent.status, thoughtChain: agent.thoughtChain })
      }
    })
    setHaltedAgentStates(statesToSave)
    setIsHalted(true)
    
    setAgents(prev => prev.map(agent => ({
      ...agent,
      status: agent.status === "running" || agent.status === "booting" ? "warning" as AgentStatus : agent.status,
      thoughtChain: agent.status === "running" || agent.status === "booting" 
        ? "HALTED - Operations suspended. Click RESUME to continue." 
        : agent.thoughtChain
    })))
  }, [agents])

  const handleResume = useCallback(() => {
    setAgents(prev => prev.map(agent => {
      const savedState = haltedAgentStates.get(agent.id)
      if (savedState && agent.status === "warning") {
        return {
          ...agent,
          status: savedState.status,
          thoughtChain: `Resuming: ${savedState.thoughtChain}`
        }
      }
      return agent
    }))
    setHaltedAgentStates(new Map())
    setIsHalted(false)
  }, [haltedAgentStates])

  const hasRunningAgents = agents.some(a => a.status === "running" || a.status === "booting")

  // Source Control handlers
  const handleTether = useCallback((repoId: string, agentId: string) => {
    // Update agent to show tethered workspace
    setAgents(prev => prev.map(agent => 
      agent.id === agentId 
        ? { ...agent, thoughtChain: `Workspace tethered: ${repoId}. Running git clone...` }
        : agent
    ))
  }, [])

  const handleDetether = useCallback((repoId: string) => {
    // Find and update the agent that was tethered to this repo
    setAgents(prev => prev.map(agent => ({
      ...agent,
      thoughtChain: agent.thoughtChain.includes(repoId) 
        ? "Workspace detethered. Standing by..."
        : agent.thoughtChain
    })))
  }, [])

  const handleCreateRepo = useCallback((name: string, targetAgentId?: string) => {
    if (targetAgentId) {
      setAgents(prev => prev.map(agent => 
        agent.id === targetAgentId 
          ? { 
              ...agent, 
              status: "running" as AgentStatus,
              thoughtChain: `Genesis initialized: ${name}. Generating project structure...` 
            }
          : agent
      ))
    }
  }, [])

  const handleAddRepo = useCallback((repo: Repository) => {
    // Log for external integration
    console.log("[v0] Repository added:", repo.name, repo.url)
  }, [])

  const handleRemoveRepo = useCallback((repoId: string) => {
    // Log for external integration  
    console.log("[v0] Repository removed:", repoId)
  }, [])

  // Task Backlog handlers
  const handleAssignTask = useCallback((taskId: string, agentId: string) => {
    // Update agent
    setAgents(prev => prev.map(agent => 
      agent.id === agentId 
        ? { 
            ...agent, 
            status: "running" as AgentStatus,
            thoughtChain: `Task assigned: Processing task ${taskId}...`
          }
        : agent
    ))
    // Update task
    setTasks(prev => prev.map(task =>
      task.id === taskId
        ? { ...task, status: "assigned" as Task["status"], assignedAgentId: agentId }
        : task
    ))
  }, [])

  const handleCreateAgentForTask = useCallback((type: Agent["type"], taskId?: string) => {
    const id = `agent-${Date.now()}`
    const newAgent: Agent = {
      id,
      name: `${type.toUpperCase()}_AGENT_${Math.floor(Math.random() * 100).toString().padStart(2, "0")}`,
      type,
      status: taskId ? "running" : "idle",
      progress: { current: 0, total: Math.floor(Math.random() * 6) + 4 },
      thoughtChain: taskId ? `Spawned for task ${taskId}. Initializing...` : "Agent spawned. Awaiting task assignment...",
    }
    setAgents(prev => [...prev, newAgent])
    
    // If spawned for a task, assign it
    if (taskId) {
      setTasks(prev => prev.map(task =>
        task.id === taskId
          ? { ...task, status: "assigned" as Task["status"], assignedAgentId: id }
          : task
      ))
    }
  }, [])
  
  // Orchestrator-specific handlers
  const handleForceAssign = useCallback((taskId: string, agentId: string) => {
    // Force assignment overrides any current agent state
    setAgents(prev => prev.map(agent => 
      agent.id === agentId 
        ? { 
            ...agent, 
            status: "running" as AgentStatus,
            thoughtChain: `FORCE ASSIGNED: Task ${taskId} - Priority override by user.`
          }
        : agent
    ))
    setTasks(prev => prev.map(task =>
      task.id === taskId
        ? { ...task, status: "assigned" as Task["status"], assignedAgentId: agentId }
        : task
    ))
  }, [])
  
  const handleCompleteTask = useCallback((taskId: string) => {
    setTasks(prev => prev.map(task =>
      task.id === taskId
        ? { ...task, status: "completed" as Task["status"], completedAt: new Date().toISOString() }
        : task
    ))
  }, [])

  const handleUpdateAgentFromTask = useCallback((agentId: string, status: AgentStatus, thoughtChain?: string) => {
    setAgents(prev => prev.map(agent => 
      agent.id === agentId 
        ? { 
            ...agent, 
            status,
            thoughtChain: thoughtChain || agent.thoughtChain
          }
        : agent
    ))
  }, [])

  // Digital Materialization handlers
  const handleMaterializeAgent = useCallback((type: Agent["type"]): string => {
    const id = `agent-${Date.now()}`
    // Agent will be fully created by MaterializingAgentCard
    return id
  }, [])

  const handleMaterializeComplete = useCallback((agentId: string) => {
    // Find the type from the agentId pattern (we stored it temporarily)
    // For now, we'll create a default agent when materialization completes
    const types: Agent["type"][] = ["firmware", "software", "validator", "reporter"]
    const randomType = types[Math.floor(Math.random() * types.length)]
    const template = agentTemplates[randomType] || agentTemplates.custom
    
    const newAgent: Agent = {
      id: agentId,
      name: `${randomType.toUpperCase()}_AGENT_${Math.floor(Math.random() * 100).toString().padStart(2, "0")}`,
      type: randomType,
      status: "idle" as AgentStatus,
      progress: { current: 0, total: Math.floor(Math.random() * 6) + 4 },
      thoughtChain: "Materialization complete. Standing by for orders...",
    }
    setAgents(prev => [...prev, newAgent])
  }, [])

  // Helper to add message to orchestrator
  const addOrchestratorMessage = (role: OrchestratorMessage["role"], content: string) => {
    const message: OrchestratorMessage = {
      id: `msg-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
      role,
      content,
      timestamp: formatTime()
    }
    setOrchestratorMessages(prev => [...prev, message])
  }

  const handleCommand = (command: string) => {
    const cmd = command.toLowerCase().trim()
    
    // Add user command to orchestrator
    addOrchestratorMessage("user", command)
    
    // Semantic agent generation - detect intent from natural language
    const detectAgentType = (text: string): Agent["type"] => {
      if (text.includes("firmware") || text.includes("driver") || text.includes("hardware") || text.includes("flash") || text.includes("embedded")) {
        return "firmware"
      }
      if (text.includes("test") || text.includes("validat") || text.includes("check") || text.includes("verify") || text.includes("qa")) {
        return "validator"
      }
      if (text.includes("report") || text.includes("document") || text.includes("summary") || text.includes("analyze") || text.includes("metrics")) {
        return "reporter"
      }
      if (text.includes("code") || text.includes("software") || text.includes("build") || text.includes("compile") || text.includes("algorithm")) {
        return "software"
      }
      return "custom"
    }
    
    // Parse commands for agent management
    if (cmd.startsWith("add ") || cmd.startsWith("spawn ") || cmd.startsWith("create ")) {
      // Check for specific type first
      const typePart = cmd.split(" ")[1]
      let agentType: Agent["type"]
      let thoughtChain: string
      
      if (agentTemplates[typePart]) {
        agentType = typePart as Agent["type"]
        thoughtChain = agentTemplates[typePart].thoughtChain || "Initializing..."
      } else {
        // Semantic detection from the full command
        agentType = detectAgentType(cmd)
        thoughtChain = `Semantic spawn: "${command}". Initializing ${agentType} protocols...`
      }
      
      const id = `agent-${Date.now()}`
      const agentName = `${agentType.toUpperCase()}_AGENT_${Math.floor(Math.random() * 100).toString().padStart(2, "0")}`
      const newAgent: Agent = {
        id,
        name: agentName,
        type: agentType,
        status: "booting" as AgentStatus,
        progress: { current: 0, total: Math.floor(Math.random() * 6) + 4 },
        thoughtChain,
      }
      setAgents(prev => [...prev, newAgent])
      
      // Send orchestrator response
      setTimeout(() => {
        addOrchestratorMessage("orchestrator", `Spawning ${agentType.toUpperCase()} agent: ${agentName}. Initializing boot sequence...`)
      }, 200)
      
      // Transition to running after boot
      setTimeout(() => {
        setAgents(prev => prev.map(a => 
          a.id === id ? { ...a, status: "running" as AgentStatus, thoughtChain: `Processing: ${command}` } : a
        ))
        addOrchestratorMessage("system", `Agent ${agentName} is now ONLINE and processing task.`)
      }, 1500)
      
    } else if (cmd.startsWith("remove ") || cmd.startsWith("kill ") || cmd.startsWith("terminate ")) {
      const namePart = cmd.split(" ").slice(1).join(" ").toUpperCase()
      const removedAgents = agents.filter(agent => agent.name.includes(namePart))
      setAgents(prev => prev.filter(agent => !agent.name.includes(namePart)))
      
      setTimeout(() => {
        if (removedAgents.length > 0) {
          addOrchestratorMessage("orchestrator", `Terminated ${removedAgents.length} agent(s): ${removedAgents.map(a => a.name).join(", ")}`)
        } else {
          addOrchestratorMessage("orchestrator", `No agents found matching "${namePart}". No agents terminated.`)
        }
      }, 200)
      
    } else if (cmd === "clear" || cmd === "reset") {
      const count = agents.length
      setAgents([])
      setTimeout(() => {
        addOrchestratorMessage("orchestrator", `All ${count} agents have been terminated. System reset complete.`)
      }, 200)
      
    } else if (cmd === "restore" || cmd === "default") {
      setAgents(defaultAgents)
      setTimeout(() => {
        addOrchestratorMessage("orchestrator", `System restored to default configuration. ${defaultAgents.length} agents initialized.`)
      }, 200)
      
    } else if (cmd.includes("agent") && (cmd.includes("test") || cmd.includes("wifi") || cmd.includes("throughput") || cmd.includes("responsible"))) {
      // Natural language agent creation
      const agentType = detectAgentType(cmd)
      const id = `agent-${Date.now()}`
      const agentName = `${agentType.toUpperCase()}_AGENT_${Math.floor(Math.random() * 100).toString().padStart(2, "0")}`
      const newAgent: Agent = {
        id,
        name: agentName,
        type: agentType,
        status: "booting" as AgentStatus,
        progress: { current: 0, total: Math.floor(Math.random() * 6) + 4 },
        thoughtChain: `Orchestrator detected: "${command}". Spawning ${agentType} agent...`,
      }
      setAgents(prev => [...prev, newAgent])
      
      setTimeout(() => {
        addOrchestratorMessage("orchestrator", `Intent recognized. Spawning ${agentType.toUpperCase()} agent: ${agentName} to handle: "${command}"`)
      }, 200)
      
      setTimeout(() => {
        setAgents(prev => prev.map(a => 
          a.id === id ? { ...a, status: "running" as AgentStatus, thoughtChain: `Task initialized: ${command}` } : a
        ))
        addOrchestratorMessage("system", `Agent ${agentName} is now ONLINE.`)
      }, 1500)
      
    } else if (cmd === "status" || cmd === "info") {
      const running = agents.filter(a => a.status === "running").length
      const idle = agents.filter(a => a.status === "idle").length
      const errors = agents.filter(a => a.status === "error").length
      setTimeout(() => {
        addOrchestratorMessage("orchestrator", `System Status: ${agents.length} total agents | ${running} running | ${idle} idle | ${errors} errors | ${tasks.filter(t => t.status === "backlog").length} pending tasks`)
      }, 200)
      
    } else if (cmd === "help") {
      setTimeout(() => {
        addOrchestratorMessage("orchestrator", `Available commands:\n• spawn/add/create [type] - Create new agent\n• remove/kill [name] - Terminate agent\n• clear/reset - Remove all agents\n• restore/default - Restore default agents\n• status/info - Show system status\n• help - Show this message\n\nAgent types: firmware, software, validator, reporter, custom`)
      }, 200)
      
    } else {
      // Unknown command
      setTimeout(() => {
        addOrchestratorMessage("orchestrator", `Command not recognized: "${command}". Type "help" for available commands, or describe what you need in natural language.`)
      }, 200)
    }
  }

  const handleSpecChange = (path: string[], newValue: string | number) => {
    console.log("[v0] Spec changed:", path.join("."), "=", newValue)
  }

  // Render panel based on active selection (for mobile/tablet)
  const renderPanel = (panelId: PanelId) => {
    switch (panelId) {
      case "host":
        return <HostDevicePanel />
      case "spec":
        return <SpecNode onSpecChange={handleSpecChange} />
      case "agents":
        return (
          <AgentMatrixWall 
            agents={agents}
            onAddAgent={handleAddAgent}
            onRemoveAgent={handleRemoveAgent}
            onConfirmAgent={handleConfirmAgent}
            onRejectAgent={handleRejectAgent}
            onRetryAgent={handleRetryAgent}
            onMaterializeAgent={handleMaterializeAgent}
            onMaterializeComplete={handleMaterializeComplete}
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
          />
        )
      case "tasks":
        return (
          <TaskBacklog
            agents={agents}
            onAssignTask={handleAssignTask}
            onCreateAgent={handleCreateAgentForTask}
            onUpdateAgentStatus={handleUpdateAgentFromTask}
          />
        )
      case "source":
        return (
          <SourceControlMatrix 
            agents={agents}
            onTether={handleTether}
            onDetether={handleDetether}
            onCreateRepo={handleCreateRepo}
            onAddRepo={handleAddRepo}
            onRemoveRepo={handleRemoveRepo}
          />
        )
      case "vitals":
        return <VitalsArtifactsPanel />
      default:
        return null
    }
  }

  return (
    <div className="relative min-h-screen flex flex-col overflow-hidden">
      {/* Neural Grid Background */}
      <NeuralGrid />
      
      {/* Main Content */}
      <div className="relative z-10 flex flex-col h-screen">
        {/* Global Status Header */}
        <GlobalStatusHeader
          finished={32 + syncCount}
          total={48}
          inProgress={agents.filter(a => a.status === "running").length}
          wslStatus="OK"
          usbStatus="Dev_01 USB Attached"
          onEmergencyStop={handleEmergencyStop}
          onResume={handleResume}
          isHalted={isHalted}
          hasRunningAgents={hasRunningAgents || isHalted}
        />
        
        {/* ===== MOBILE LAYOUT (< 768px) ===== */}
        <main className="flex-1 flex flex-col md:hidden min-h-0 pb-24">
          <div className="flex-1 p-3 overflow-auto">
            {renderPanel(activePanel)}
          </div>
        </main>
        
        {/* ===== TABLET LAYOUT (768px - 1023px) ===== */}
        <main className="hidden md:flex lg:hidden flex-1 min-h-0">
          <TabletNav activePanel={activePanel} onPanelChange={setActivePanel} />
          <div className="flex-1 p-3 overflow-auto">
            {renderPanel(activePanel)}
          </div>
        </main>
        
        {/* ===== DESKTOP LAYOUT (>= 1024px) ===== */}
        <main className="hidden lg:grid flex-1 grid-cols-[200px_220px_1fr_280px_220px_220px_280px] gap-3 p-4 min-h-0">
          {/* Far Left: Host & Devices */}
          <aside className="min-h-0 overflow-hidden">
            <HostDevicePanel />
          </aside>
          
          {/* Left: SPEC Node */}
          <aside className="min-h-0">
            <SpecNode onSpecChange={handleSpecChange} />
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
              onMaterializeAgent={handleMaterializeAgent}
              onMaterializeComplete={handleMaterializeComplete}
            />
          </section>
          
          {/* Orchestrator AI - Central Coordinator & Command Hub */}
          <aside className="min-h-0 overflow-hidden">
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
            />
          </aside>
          
          {/* Task Backlog */}
          <aside className="min-h-0 overflow-hidden">
            <TaskBacklog
              agents={agents}
              onAssignTask={handleAssignTask}
              onCreateAgent={handleCreateAgentForTask}
              onUpdateAgentStatus={handleUpdateAgentFromTask}
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
          
          {/* Far Right: Vitals & Artifacts */}
          <aside className="min-h-0 overflow-hidden">
            <VitalsArtifactsPanel />
          </aside>
        </main>
        
        {/* Bottom: Invoke Core - Desktop */}
        <footer className="relative z-20 hidden lg:block">
          <InvokeCore onInvoke={handleInvoke} onCommand={handleCommand} />
        </footer>
        
        {/* Bottom: Invoke Core - Mobile/Tablet (above navigation) */}
        <footer className="relative z-20 lg:hidden mb-20 md:mb-0">
          <InvokeCore onInvoke={handleInvoke} onCommand={handleCommand} />
        </footer>
      </div>
      
      {/* Mobile Bottom Navigation */}
      <MobileNav activePanel={activePanel} onPanelChange={setActivePanel} />
    </div>
  )
}
