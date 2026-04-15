"use client"

import { useState, useCallback, useRef, useEffect } from "react"
import { PanelHelp } from "@/components/omnisight/panel-help"
import {
  GitBranch,
  GitCommit,
  Link2,
  Unlink,
  Zap,
  FolderGit2,
  ExternalLink,
  Check,
  X,
  Loader2,
  FileCode,
  FileText,
  FolderTree,
  Sparkles,
  Trash2,
  Download
} from "lucide-react"
import type { Agent } from "./agent-matrix-wall"

export interface Repository {
  id: string
  name: string
  url: string
  branch: string
  // `unconfigured` covers a workspace with no remote yet (pre-tether);
  // widening here matches the engine/useEngine shape so app/page.tsx
  // doesn't need a coercion layer. UI paths already handle it.
  status: "synced" | "syncing" | "error" | "detached" | "unconfigured"
  lastCommit?: string
  lastCommitTime?: string
  tetheredAgentId?: string
}

interface DataPipeline {
  id: string
  repoId: string
  agentId: string
  status: "establishing" | "active" | "transferring" | "error"
}

interface SourceControlMatrixProps {
  agents: Agent[]
  repositories?: Repository[]
  onTether?: (repoId: string, agentId: string) => void
  onDetether?: (repoId: string) => void
  onCreateRepo?: (name: string, targetAgentId?: string) => void
  onAddRepo?: (repo: Repository) => void
  onRemoveRepo?: (repoId: string) => void
  onReposChange?: (repos: Repository[]) => void
}

// Empty default — real repos come from backend via GET /system/repos
const defaultRepos: Repository[] = []

export function SourceControlMatrix({ 
  agents, 
  repositories,
  onTether, 
  onDetether, 
  onCreateRepo,
  onAddRepo,
  onRemoveRepo,
  onReposChange
}: SourceControlMatrixProps) {
  const [internalRepos, setInternalRepos] = useState<Repository[]>(defaultRepos)
  
  // Use external repos if provided, otherwise use internal state
  const repos = repositories ?? internalRepos
  const setRepos = useCallback((updater: Repository[] | ((prev: Repository[]) => Repository[])) => {
    const newRepos = typeof updater === 'function' ? updater(repos) : updater
    if (repositories === undefined) {
      setInternalRepos(newRepos)
    }
    onReposChange?.(newRepos)
  }, [repositories, repos, onReposChange])
  // Build pipelines dynamically from repos with tethered agents
  const [pipelines, setPipelines] = useState<DataPipeline[]>([])
  useEffect(() => {
    const pipes = repos
      .filter(r => r.tetheredAgentId)
      .map((r, i) => ({
        id: `pipe-${i}`,
        repoId: r.id,
        agentId: r.tetheredAgentId!,
        status: "active" as const,
      }))
    setPipelines(pipes) // eslint-disable-line react-hooks/set-state-in-effect -- derived state from repos
  }, [repos])
  const [selectedRepo, setSelectedRepo] = useState<string | null>(null)
  const [tetheringMode, setTetheringMode] = useState(false)
  const [showGenesisModal, setShowGenesisModal] = useState(false)
  const [newRepoName, setNewRepoName] = useState("")
  const [targetAgent, setTargetAgent] = useState<string>("")
  const [isCreating, setIsCreating] = useState(false)
  const [beamTarget, setBeamTarget] = useState<string | null>(null)
  const [showAddRepoModal, setShowAddRepoModal] = useState(false)
  const [newRepoUrl, setNewRepoUrl] = useState("")
  const [newRepoBranch, setNewRepoBranch] = useState("main")
  const [isCloning, setIsCloning] = useState(false)
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null)
  
  const matrixRef = useRef<HTMLDivElement>(null)

  // Handle tethering a repo to an agent
  const handleTether = useCallback((repoId: string, agentId: string) => {
    // Create establishing pipeline
    const pipelineId = `pipe-${Date.now()}`
    setPipelines(prev => [...prev, {
      id: pipelineId,
      repoId,
      agentId,
      status: "establishing"
    }])

    // Update pipeline and repo state
    setPipelines(prev => prev.map(p =>
      p.id === pipelineId ? { ...p, status: "active" } : p
    ))
    setRepos(prev => prev.map(r =>
      r.id === repoId ? { ...r, tetheredAgentId: agentId } : r
    ))

    setSelectedRepo(null)
    setTetheringMode(false)
    setBeamTarget(null)
    onTether?.(repoId, agentId)
  }, [onTether, setRepos])

  // Handle detethering
  const handleDetether = useCallback((repoId: string) => {
    setPipelines(prev => prev.filter(p => p.repoId !== repoId))
    setRepos(prev => prev.map(r =>
      r.id === repoId ? { ...r, tetheredAgentId: undefined } : r
    ))
    onDetether?.(repoId)
  }, [onDetether, setRepos])

  // Handle genesis initialization
  const handleGenesis = useCallback(() => {
    if (!newRepoName.trim()) return

    setIsCreating(true)

    const newRepo: Repository = {
      id: `repo-${Date.now()}`,
      name: newRepoName.toLowerCase().replace(/\s+/g, "_"),
      url: `local://${newRepoName.toLowerCase().replace(/\s+/g, "_")}`,
      branch: "main",
      status: "synced",
      lastCommit: "initial",
      lastCommitTime: "now"
    }

    setRepos(prev => [...prev, newRepo])
    if (targetAgent) {
      handleTether(newRepo.id, targetAgent)
    }

    setIsCreating(false)
    setShowGenesisModal(false)
    setNewRepoName("")
    setTargetAgent("")
    onCreateRepo?.(newRepoName, targetAgent || undefined)
  }, [newRepoName, targetAgent, handleTether, onCreateRepo, setRepos])

  // Handle manual repo addition
  const handleAddRepo = useCallback(() => {
    if (!newRepoUrl.trim()) return
    
    setIsCloning(true)
    
    // Extract repo name from URL
    const urlParts = newRepoUrl.split('/')
    const repoNameWithGit = urlParts[urlParts.length - 1]
    const repoName = repoNameWithGit.replace('.git', '')
    
    const newRepo: Repository = {
      id: `repo-${Date.now()}`,
      name: repoName,
      url: newRepoUrl,
      branch: newRepoBranch,
      status: "syncing",
      lastCommit: "fetching...",
      lastCommitTime: "now"
    }
    
    setRepos(prev => [...prev, newRepo])
    onAddRepo?.(newRepo)

    setIsCloning(false)
    setShowAddRepoModal(false)
    setNewRepoUrl("")
    setNewRepoBranch("main")
  }, [newRepoUrl, newRepoBranch, setRepos, onAddRepo])

  // Handle repo removal
  const handleRemoveRepo = useCallback((repoId: string) => {
    // First detether if connected
    const repo = repos.find(r => r.id === repoId)
    if (repo?.tetheredAgentId) {
      handleDetether(repoId)
    }
    
    setRepos(prev => prev.filter(r => r.id !== repoId))
    setPipelines(prev => prev.filter(p => p.repoId !== repoId))
    onRemoveRepo?.(repoId)
    setConfirmDeleteId(null)
  }, [repos, handleDetether, setRepos, onRemoveRepo])

  // Get agent display name
  const getAgentName = (agentId: string) => {
    const agent = agents.find(a => a.id === agentId)
    return agent?.name || agentId
  }

  // Get available agents (not already tethered)
  const availableAgents = agents.filter(a => 
    !repos.some(r => r.tetheredAgentId === a.id)
  )

  return (
    <div className="h-full flex flex-col" ref={matrixRef}>
      {/* Header */}
      <div className="px-3 py-2 holo-glass-simple mb-3 corner-brackets circuit-pattern">
        <div className="flex items-center gap-2 relative z-10">
          <div className="w-2 h-2 rounded-full bg-[var(--artifact-purple)] pulse-purple shrink-0 neon-border" />
          <h2 className="font-mono text-xs font-semibold tracking-fui text-[var(--artifact-purple)]">
            SOURCE
          </h2>
          <PanelHelp doc="panels-overview" />
          <div className="flex items-center gap-1 ml-auto">
            <button
              onClick={() => setShowAddRepoModal(true)}
              className="p-1.5 rounded bg-[var(--neural-blue)]/20 hover:bg-[var(--neural-blue)]/40 text-[var(--neural-blue)] transition-colors"
              title="Clone repository"
            >
              <Download size={12} />
            </button>
            <button
              onClick={() => setShowGenesisModal(true)}
              className="p-1.5 rounded bg-[var(--artifact-purple)]/20 hover:bg-[var(--artifact-purple)]/40 text-[var(--artifact-purple)] transition-colors"
              title="Genesis - Create new"
            >
              <Sparkles size={12} />
            </button>
          </div>
        </div>
        <div className="flex flex-col gap-0.5 mt-1.5 text-xs font-mono">
          <span className="text-[var(--muted-foreground)]">{repos.length} repositories</span>
          <div className="flex items-center gap-2">
            <span className="text-[var(--validation-emerald)]">{pipelines.filter(p => p.status === "active").length} tethered</span>
            <span className="text-[var(--neural-blue)]">{pipelines.filter(p => p.status === "transferring").length} syncing</span>
          </div>
        </div>
      </div>

      {/* Repository Nodes */}
      <div className="flex-1 overflow-auto space-y-3 pr-2">
        {repos.map(repo => {
          const pipeline = pipelines.find(p => p.repoId === repo.id)
          const isSelected = selectedRepo === repo.id
          const isTethered = !!repo.tetheredAgentId
          
          return (
            <div
              key={repo.id}
              className={`holo-glass-simple rounded transition-all duration-300 group ${
                isSelected ? "ring-2 ring-[var(--artifact-purple)]" : ""
              } ${isTethered ? "border-l-2 border-l-[var(--validation-emerald)]" : ""}`}
            >
              {/* Repo Header */}
              <div className="p-3">
                {/* Top Row: Icon + Name + Actions */}
                <div className="flex items-start gap-2 mb-2">
                  {/* Repo Icon */}
                  <div className={`p-1.5 rounded shrink-0 ${
                    repo.status === "synced" ? "bg-[var(--validation-emerald)]/20" :
                    repo.status === "syncing" ? "bg-[var(--neural-blue)]/20" :
                    repo.status === "error" ? "bg-[var(--critical-red)]/20" :
                    "bg-[var(--muted-foreground)]/20"
                  }`}>
                    <FolderGit2 size={14} className={
                      repo.status === "synced" ? "text-[var(--validation-emerald)]" :
                      repo.status === "syncing" ? "text-[var(--neural-blue)] animate-pulse" :
                      repo.status === "error" ? "text-[var(--critical-red)]" :
                      "text-[var(--muted-foreground)]"
                    } />
                  </div>
                  
                  {/* Repo Name */}
                  <div className="flex-1 min-w-0">
                    <span className="font-mono text-xs font-semibold text-[var(--foreground)] break-all" title={repo.name}>
                      {repo.name}
                    </span>
                  </div>
                  
                  {/* Actions */}
                  <div className="flex items-center gap-1 shrink-0">
                    {repo.status === "syncing" && (
                      <Loader2 size={12} className="text-[var(--neural-blue)] animate-spin" />
                    )}
                    {isTethered ? (
                      <button
                        onClick={() => handleDetether(repo.id)}
                        className="p-1 rounded bg-[var(--critical-red)]/20 hover:bg-[var(--critical-red)]/40 text-[var(--critical-red)] transition-colors"
                        title="Detether from agent"
                      >
                        <Unlink size={11} />
                      </button>
                    ) : (
                      <button
                        onClick={() => {
                          setSelectedRepo(isSelected ? null : repo.id)
                          setTetheringMode(!isSelected)
                        }}
                        className={`p-1 rounded transition-colors ${
                          isSelected 
                            ? "bg-[var(--artifact-purple)]/40 text-[var(--artifact-purple)]" 
                            : "bg-[var(--artifact-purple)]/20 hover:bg-[var(--artifact-purple)]/40 text-[var(--artifact-purple)]"
                        }`}
                        title="Tether to agent"
                      >
                        <Link2 size={11} />
                      </button>
                    )}
                    {confirmDeleteId === repo.id ? (
                      <div className="flex items-center gap-0.5">
                        <button
                          onClick={() => handleRemoveRepo(repo.id)}
                          className="p-1 rounded bg-[var(--critical-red)] text-white transition-colors"
                          title="Confirm delete"
                        >
                          <Check size={11} />
                        </button>
                        <button
                          onClick={() => setConfirmDeleteId(null)}
                          className="p-1 rounded bg-[var(--secondary)] text-[var(--muted-foreground)] hover:text-[var(--foreground)] transition-colors"
                          title="Cancel"
                        >
                          <X size={11} />
                        </button>
                      </div>
                    ) : (
                      <button
                        onClick={() => setConfirmDeleteId(repo.id)}
                        className="p-1 rounded opacity-0 group-hover:opacity-100 bg-[var(--critical-red)]/20 hover:bg-[var(--critical-red)]/40 text-[var(--critical-red)] transition-all"
                        title="Remove repository"
                      >
                        <Trash2 size={11} />
                      </button>
                    )}
                  </div>
                </div>
                
                {/* Second Row: Branch + Status */}
                <div className="flex items-center gap-2 mb-1.5 pl-7">
                  <div className="flex items-center gap-1">
                    <GitBranch size={10} className="text-[var(--muted-foreground)]" />
                    <span className="font-mono text-[10px] px-1.5 py-0.5 rounded bg-[var(--secondary)] text-[var(--artifact-purple)]">
                      {repo.branch}
                    </span>
                  </div>
                  <span className={`font-mono text-[10px] px-1.5 py-0.5 rounded ${
                    repo.status === "synced" ? "bg-[var(--validation-emerald)]/20 text-[var(--validation-emerald)]" :
                    repo.status === "syncing" ? "bg-[var(--neural-blue)]/20 text-[var(--neural-blue)]" :
                    repo.status === "error" ? "bg-[var(--critical-red)]/20 text-[var(--critical-red)]" :
                    "bg-[var(--muted-foreground)]/20 text-[var(--muted-foreground)]"
                  }`}>
                    {repo.status.toUpperCase()}
                  </span>
                </div>
                
                {/* Third Row: Commit Info */}
                <div className="flex items-center gap-2 pl-7">
                  <div className="flex items-center gap-1">
                    <GitCommit size={10} className="text-[var(--muted-foreground)]" />
                    <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
                      {repo.lastCommit}
                    </span>
                  </div>
                  <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
                    {repo.lastCommitTime}
                  </span>
                </div>
                
                {/* Fourth Row: URL (truncated with tooltip) */}
                <div className="flex items-center gap-1 mt-1.5 pl-7">
                  <ExternalLink size={10} className="text-[var(--muted-foreground)] shrink-0" />
                  <span 
                    className="font-mono text-[10px] text-[var(--muted-foreground)] truncate cursor-help"
                    title={repo.url}
                  >
                    {repo.url.replace('git@github.com:', '').replace('.git', '')}
                  </span>
                </div>
              </div>
              
              {/* Data Pipeline Visualization */}
              {pipeline && (
                <div className={`px-3 pb-3 ${
                  pipeline.status === "establishing" ? "animate-pulse" : ""
                }`}>
                  <div className="flex items-center gap-2 p-2 rounded bg-[var(--secondary)]">
                    {/* Pipeline Beam */}
                    <div className="flex-1 h-1 rounded-full overflow-hidden bg-[var(--border)]">
                      <div 
                        className={`h-full transition-all duration-1000 ${
                          pipeline.status === "establishing" 
                            ? "w-1/3 bg-[var(--artifact-purple)] animate-pulse" :
                          pipeline.status === "active" 
                            ? "w-full bg-[var(--validation-emerald)]" :
                          pipeline.status === "transferring" 
                            ? "w-2/3 bg-[var(--neural-blue)] animate-pulse" :
                          "w-0 bg-[var(--critical-red)]"
                        }`}
                      />
                    </div>
                    <Zap size={10} className={`shrink-0 ${
                      pipeline.status === "active" ? "text-[var(--validation-emerald)]" :
                      pipeline.status === "transferring" ? "text-[var(--neural-blue)] animate-pulse" :
                      "text-[var(--artifact-purple)]"
                    }`} />
                    <span className="font-mono text-xs text-[var(--foreground)] truncate">
                      {getAgentName(pipeline.agentId)}
                    </span>
                  </div>
                </div>
              )}
              
              {/* Tethering Target Selection */}
              {isSelected && tetheringMode && (
                <div className="px-3 pb-3 border-t border-[var(--border)] pt-3">
                  <p className="font-mono text-xs text-[var(--artifact-purple)] mb-2">
                    SELECT TARGET AGENT FOR DATA TETHERING:
                  </p>
                  <div className="space-y-2">
                    {availableAgents.length === 0 ? (
                      <p className="font-mono text-xs text-[var(--muted-foreground)]">
                        No available agents. All agents are tethered.
                      </p>
                    ) : (
                      availableAgents.map(agent => (
                        <button
                          key={agent.id}
                          onClick={() => handleTether(repo.id, agent.id)}
                          onMouseEnter={() => setBeamTarget(agent.id)}
                          onMouseLeave={() => setBeamTarget(null)}
                          className={`w-full flex items-center gap-2 p-2 rounded transition-all ${
                            beamTarget === agent.id 
                              ? "bg-[var(--artifact-purple)]/30 ring-1 ring-[var(--artifact-purple)]" 
                              : "bg-[var(--secondary)] hover:bg-[var(--artifact-purple)]/20"
                          }`}
                        >
                          <div className={`w-2 h-2 rounded-full shrink-0 ${
                            agent.status === "running" ? "bg-[var(--neural-blue)] animate-pulse" :
                            agent.status === "success" ? "bg-[var(--validation-emerald)]" :
                            agent.status === "error" ? "bg-[var(--critical-red)]" :
                            "bg-[var(--muted-foreground)]"
                          }`} />
                          <span className="font-mono text-xs text-[var(--foreground)] flex-1 text-left truncate">
                            {agent.name}
                          </span>
                          {beamTarget === agent.id && (
                            <span className="font-mono text-xs text-[var(--artifact-purple)] shrink-0">
                              TETHER
                            </span>
                          )}
                        </button>
                      ))
                    )}
                  </div>
                </div>
              )}
            </div>
          )
        })}
      </div>

      {/* Genesis Initialization Modal */}
      {showGenesisModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
          <div className="w-full max-w-md holo-glass rounded-lg overflow-hidden animate-in fade-in zoom-in-95 duration-300">
            {/* Modal Header */}
            <div className="px-6 py-4 border-b border-[var(--border)] bg-[var(--artifact-purple)]/10">
              <div className="flex items-center gap-3">
                <div className="p-2 rounded-full bg-[var(--artifact-purple)]/20">
                  <Sparkles size={20} className="text-[var(--artifact-purple)]" />
                </div>
                <div>
                  <h3 className="font-sans text-lg font-semibold tracking-fui text-[var(--artifact-purple)]">
                    GENESIS INITIALIZATION
                  </h3>
                  <p className="font-mono text-xs text-[var(--muted-foreground)]">
                    Initialize a new repository core
                  </p>
                </div>
              </div>
            </div>
            
            {/* Modal Body */}
            <div className="px-6 py-4 space-y-4">
              {/* Project Name */}
              <div>
                <label className="block font-mono text-xs text-[var(--muted-foreground)] mb-2">
                  PROJECT DESIGNATION
                </label>
                <input
                  type="text"
                  value={newRepoName}
                  onChange={(e) => setNewRepoName(e.target.value)}
                  placeholder="Enter project name..."
                  className="w-full px-3 py-2 rounded bg-[var(--secondary)] border border-[var(--border)] font-mono text-sm text-[var(--foreground)] placeholder:text-[var(--muted-foreground)] focus:outline-none focus:ring-2 focus:ring-[var(--artifact-purple)]"
                  disabled={isCreating}
                />
              </div>
              
              {/* Target Agent (Optional) */}
              <div>
                <label className="block font-mono text-xs text-[var(--muted-foreground)] mb-2">
                  AUTO-TETHER TO ORCHESTRATOR (OPTIONAL)
                </label>
                <select
                  value={targetAgent}
                  onChange={(e) => setTargetAgent(e.target.value)}
                  className="w-full px-3 py-2 rounded bg-[var(--secondary)] border border-[var(--border)] font-mono text-sm text-[var(--foreground)] focus:outline-none focus:ring-2 focus:ring-[var(--artifact-purple)]"
                  disabled={isCreating}
                >
                  <option value="">No auto-tether</option>
                  {agents.map(agent => (
                    <option key={agent.id} value={agent.id}>
                      {agent.name}
                    </option>
                  ))}
                </select>
              </div>
              
              {/* Generated Structure Preview */}
              {newRepoName && (
                <div className="p-3 rounded bg-[var(--secondary)] border border-[var(--border)]">
                  <p className="font-mono text-xs text-[var(--muted-foreground)] mb-2">
                    AUTO-GENERATED STRUCTURE:
                  </p>
                  <div className="space-y-1 font-mono text-xs">
                    <div className="flex items-center gap-2 text-[var(--foreground)]">
                      <FolderTree size={12} className="text-[var(--artifact-purple)]" />
                      <span>{newRepoName.toLowerCase().replace(/\s+/g, "_")}/</span>
                    </div>
                    <div className="flex items-center gap-2 text-[var(--muted-foreground)] pl-4">
                      <FileText size={10} />
                      <span>README.md</span>
                    </div>
                    <div className="flex items-center gap-2 text-[var(--muted-foreground)] pl-4">
                      <FileCode size={10} />
                      <span>Makefile</span>
                    </div>
                    <div className="flex items-center gap-2 text-[var(--muted-foreground)] pl-4">
                      <FileText size={10} />
                      <span>.gitignore</span>
                    </div>
                    <div className="flex items-center gap-2 text-[var(--muted-foreground)] pl-4">
                      <FolderTree size={10} />
                      <span>src/</span>
                    </div>
                    <div className="flex items-center gap-2 text-[var(--muted-foreground)] pl-4">
                      <FolderTree size={10} />
                      <span>include/</span>
                    </div>
                    <div className="flex items-center gap-2 text-[var(--muted-foreground)] pl-4">
                      <FolderTree size={10} />
                      <span>tests/</span>
                    </div>
                  </div>
                </div>
              )}
            </div>
            
            {/* Modal Footer */}
            <div className="px-6 py-4 border-t border-[var(--border)] flex items-center justify-end gap-3">
              <button
                onClick={() => {
                  setShowGenesisModal(false)
                  setNewRepoName("")
                  setTargetAgent("")
                }}
                disabled={isCreating}
                className="px-4 py-2 rounded font-mono text-xs text-[var(--muted-foreground)] hover:text-[var(--foreground)] hover:bg-[var(--secondary)] transition-colors disabled:opacity-50"
              >
                ABORT
              </button>
              <button
                onClick={handleGenesis}
                disabled={!newRepoName.trim() || isCreating}
                className="flex items-center gap-2 px-4 py-2 rounded font-mono text-xs bg-[var(--artifact-purple)] hover:bg-[var(--artifact-purple)]/80 text-white transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {isCreating ? (
                  <>
                    <Loader2 size={12} className="animate-spin" />
                    INITIALIZING...
                  </>
                ) : (
                  <>
                    <Zap size={12} />
                    INITIALIZE CORE
                  </>
                )}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Add Repository Modal */}
      {showAddRepoModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
          <div className="w-full max-w-md holo-glass rounded-lg overflow-hidden animate-in fade-in zoom-in-95 duration-300">
            {/* Modal Header */}
            <div className="px-6 py-4 border-b border-[var(--border)] bg-[var(--neural-blue)]/10">
              <div className="flex items-center gap-3">
                <div className="p-2 rounded-full bg-[var(--neural-blue)]/20">
                  <Download size={20} className="text-[var(--neural-blue)]" />
                </div>
                <div>
                  <h3 className="font-sans text-lg font-semibold tracking-fui text-[var(--neural-blue)]">
                    CLONE REPOSITORY
                  </h3>
                  <p className="font-mono text-xs text-[var(--muted-foreground)]">
                    Add existing repository to workspace
                  </p>
                </div>
              </div>
            </div>
            
            {/* Modal Body */}
            <div className="px-6 py-4 space-y-4">
              {/* Repository URL */}
              <div>
                <label className="block font-mono text-xs text-[var(--muted-foreground)] mb-2">
                  REPOSITORY URL
                </label>
                <input
                  type="text"
                  value={newRepoUrl}
                  onChange={(e) => setNewRepoUrl(e.target.value)}
                  placeholder="git@github.com:user/repo.git"
                  className="w-full px-3 py-2 rounded bg-[var(--secondary)] border border-[var(--border)] font-mono text-sm text-[var(--foreground)] placeholder:text-[var(--muted-foreground)] focus:outline-none focus:ring-2 focus:ring-[var(--neural-blue)]"
                  disabled={isCloning}
                />
              </div>
              
              {/* Branch */}
              <div>
                <label className="block font-mono text-xs text-[var(--muted-foreground)] mb-2">
                  BRANCH
                </label>
                <input
                  type="text"
                  value={newRepoBranch}
                  onChange={(e) => setNewRepoBranch(e.target.value)}
                  placeholder="main"
                  className="w-full px-3 py-2 rounded bg-[var(--secondary)] border border-[var(--border)] font-mono text-sm text-[var(--foreground)] placeholder:text-[var(--muted-foreground)] focus:outline-none focus:ring-2 focus:ring-[var(--neural-blue)]"
                  disabled={isCloning}
                />
              </div>
              
              {/* Preview */}
              {newRepoUrl && (
                <div className="p-3 rounded bg-[var(--secondary)] border border-[var(--border)]">
                  <p className="font-mono text-xs text-[var(--muted-foreground)] mb-2">
                    CLONE PREVIEW:
                  </p>
                  <div className="flex items-center gap-2">
                    <FolderGit2 size={14} className="text-[var(--neural-blue)]" />
                    <span className="font-mono text-sm text-[var(--foreground)]">
                      {newRepoUrl.split('/').pop()?.replace('.git', '') || 'repository'}
                    </span>
                    <span className="font-mono text-xs px-1.5 py-0.5 rounded bg-[var(--neural-blue)]/20 text-[var(--neural-blue)]">
                      {newRepoBranch}
                    </span>
                  </div>
                </div>
              )}
            </div>
            
            {/* Modal Footer */}
            <div className="px-6 py-4 border-t border-[var(--border)] flex items-center justify-end gap-3">
              <button
                onClick={() => {
                  setShowAddRepoModal(false)
                  setNewRepoUrl("")
                  setNewRepoBranch("main")
                }}
                disabled={isCloning}
                className="px-4 py-2 rounded font-mono text-xs text-[var(--muted-foreground)] hover:text-[var(--foreground)] hover:bg-[var(--secondary)] transition-colors disabled:opacity-50"
              >
                CANCEL
              </button>
              <button
                onClick={handleAddRepo}
                disabled={!newRepoUrl.trim() || isCloning}
                className="flex items-center gap-2 px-4 py-2 rounded font-mono text-xs bg-[var(--neural-blue)] hover:bg-[var(--neural-blue)]/80 text-white transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {isCloning ? (
                  <>
                    <Loader2 size={12} className="animate-spin" />
                    CLONING...
                  </>
                ) : (
                  <>
                    <Download size={12} />
                    CLONE REPO
                  </>
                )}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
