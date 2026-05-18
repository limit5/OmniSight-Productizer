"use client"

import { useCallback, useEffect, useState } from "react"
import { BookOpen, Loader2, Play } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Block } from "@/components/omnisight/block"
import {
  executeRunbook,
  listEffectiveRunbooks,
  type ExecuteRunbookRequest,
  type ExecuteRunbookResponse,
  type RunbookSummary,
} from "@/lib/api"
import { cn } from "@/lib/utils"

export interface RunbookPanelProps {
  tenantId: string
  userId?: string
  projectId?: string
  sessionId?: string
  className?: string
  fetchRunbooks?: () => Promise<{ items: RunbookSummary[]; count: number }>
  runRunbook?: (
    name: string,
    body: ExecuteRunbookRequest,
  ) => Promise<ExecuteRunbookResponse>
}

const SCOPE_TONE: Record<string, "info" | "neutral" | "success"> = {
  project: "info",
  home: "neutral",
  bundled: "success",
}

export function RunbookPanel({
  tenantId,
  userId,
  projectId,
  sessionId,
  className,
  fetchRunbooks = listEffectiveRunbooks,
  runRunbook = executeRunbook,
}: RunbookPanelProps) {
  const [runbooks, setRunbooks] = useState<RunbookSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selectedName, setSelectedName] = useState<string | null>(null)
  const [paramValues, setParamValues] = useState<Record<string, string>>({})
  const [running, setRunning] = useState(false)
  const [runError, setRunError] = useState<string | null>(null)
  const [runResult, setRunResult] = useState<ExecuteRunbookResponse | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    fetchRunbooks()
      .then((res) => {
        if (!cancelled) {
          setRunbooks(res.items)
          setError(null)
        }
      })
      .catch((exc) => {
        if (!cancelled) {
          setRunbooks([])
          setError(exc instanceof Error ? exc.message : String(exc))
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [fetchRunbooks])

  const selected = runbooks.find((rb) => rb.name === selectedName) ?? null

  const handleSelect = useCallback(
    (rb: RunbookSummary) => {
      setSelectedName(rb.name)
      const seed: Record<string, string> = {}
      for (const p of rb.params) {
        seed[p.name] = p.default == null ? "" : String(p.default)
      }
      setParamValues(seed)
      setRunResult(null)
      setRunError(null)
    },
    [],
  )

  const handleRun = useCallback(async () => {
    if (!selected) return
    setRunning(true)
    setRunError(null)
    setRunResult(null)
    try {
      const resp = await runRunbook(selected.name, {
        params: { ...paramValues },
        tenant_id: tenantId,
        user_id: userId ?? null,
        project_id: projectId ?? null,
        session_id: sessionId ?? null,
      })
      setRunResult(resp)
    } catch (exc) {
      setRunError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setRunning(false)
    }
  }, [paramValues, projectId, runRunbook, selected, sessionId, tenantId, userId])

  return (
    <Block
      as="section"
      title="RUNBOOKS"
      icon={BookOpen}
      kind="wp8.runbook.panel"
      status={loading ? "loading" : "ready"}
      className={cn("gap-2", className)}
      data-testid="runbook-panel"
    >
      {loading && (
        <div
          className="flex items-center gap-2 font-mono text-[11px] text-[var(--muted-foreground)]"
          data-testid="runbook-panel-loading"
        >
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          Loading runbooks…
        </div>
      )}

      {!loading && error && (
        <div
          className="rounded-sm border border-[var(--destructive)]/40 bg-[var(--destructive)]/10 px-2 py-1.5 font-mono text-[11px] text-[var(--destructive)]"
          data-testid="runbook-panel-error"
        >
          {error}
        </div>
      )}

      {!loading && !error && runbooks.length === 0 && (
        <div
          className="rounded-sm border border-[var(--border)] px-2 py-1.5 font-mono text-[11px] text-[var(--muted-foreground)]"
          data-testid="runbook-panel-empty"
        >
          No runbooks yet. Right-click any Block and choose "Save as Runbook".
        </div>
      )}

      {!loading && !error && runbooks.length > 0 && (
        <div className="grid gap-1.5" data-testid="runbook-panel-list">
          {runbooks.map((rb) => (
            <Block
              key={rb.name}
              as="button"
              type="button"
              kind="wp8.runbook.entry"
              status={rb.scope}
              tone={SCOPE_TONE[rb.scope] ?? "neutral"}
              onClick={() => handleSelect(rb)}
              className={cn(
                "w-full text-left",
                selectedName === rb.name && "ring-1 ring-[var(--neural-blue,#3b82f6)]",
              )}
              data-testid={`runbook-entry-${rb.name}`}
            >
              <div className="font-mono text-[11px]">
                <div className="font-semibold">{rb.name}</div>
                {rb.description && (
                  <div className="text-[var(--muted-foreground)]">
                    {rb.description}
                  </div>
                )}
                <div className="text-[10px] uppercase tracking-[0.18em] text-[var(--muted-foreground)]">
                  {rb.scope} · {rb.steps.length} step
                  {rb.steps.length === 1 ? "" : "s"}
                </div>
              </div>
            </Block>
          ))}
        </div>
      )}

      {selected && (
        <Block
          as="section"
          title={`EXECUTE — ${selected.name}`}
          kind="wp8.runbook.execute"
          status="ready"
          className="gap-2"
          data-testid="runbook-execute-panel"
        >
          {selected.params.length === 0 && (
            <div className="font-mono text-[11px] text-[var(--muted-foreground)]">
              No parameters; runs immediately on Execute.
            </div>
          )}
          {selected.params.map((p) => (
            <label
              key={p.name}
              className="flex flex-col gap-1 font-mono text-[11px]"
            >
              <span>
                {p.name}{" "}
                <span className="text-[var(--muted-foreground)]">
                  ({p.type}
                  {p.required ? ", required" : ""})
                </span>
              </span>
              <input
                type="text"
                className="rounded-sm border border-[var(--border)] bg-transparent px-2 py-1"
                value={paramValues[p.name] ?? ""}
                onChange={(e) =>
                  setParamValues((prev) => ({
                    ...prev,
                    [p.name]: e.target.value,
                  }))
                }
                placeholder={
                  p.description ||
                  (p.default == null ? "" : String(p.default))
                }
                data-testid={`runbook-panel-param-${p.name}`}
              />
            </label>
          ))}
          <div className="flex justify-end">
            <Button
              type="button"
              onClick={handleRun}
              disabled={running}
              data-testid="runbook-panel-run"
            >
              {running ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Play className="h-4 w-4" />
              )}
              Execute runbook
            </Button>
          </div>
          {runError && (
            <div
              className="rounded-sm border border-[var(--destructive)]/40 bg-[var(--destructive)]/10 px-2 py-1.5 font-mono text-[11px] text-[var(--destructive)]"
              data-testid="runbook-panel-run-error"
            >
              {runError}
            </div>
          )}
          {runResult && (
            <div
              className="rounded-sm border border-[var(--validation-emerald,#10b981)]/40 bg-[var(--validation-emerald,#10b981)]/10 px-2 py-1.5 font-mono text-[11px]"
              data-testid="runbook-panel-run-result"
            >
              Produced {runResult.blocks.length} block
              {runResult.blocks.length === 1 ? "" : "s"} chained from runbook
              {" "}
              <span className="font-semibold">{runResult.runbook.name}</span>.
            </div>
          )}
        </Block>
      )}
    </Block>
  )
}
