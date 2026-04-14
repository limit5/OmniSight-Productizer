"use client"

/**
 * Phase 56-DAG-G — DAG canvas visualization (read-only).
 *
 * Renders a FormDAG as a topological SVG diagram: nodes are tasks,
 * edges are depends_on. Layout is depth-based — a node's layer is
 * `1 + max(layer[d] for d in depends_on)`. Within a layer, nodes
 * stack vertically in list order. For 1–20 node DAGs this is
 * legible enough; the upgrade to react-flow with pan/zoom/minimap
 * can wait until operators ask for it.
 *
 * Design calls, annotated:
 *   - No new deps. Pure React + SVG. Tailwind for chrome. Matches
 *     the rest of the codebase's dependency discipline.
 *   - Read-only for v1. Drag-to-connect depends_on requires a much
 *     bigger interaction model; the form tab already edits deps
 *     via chip toggles — that UX is sufficient until proven
 *     otherwise.
 *   - Cycle detection is visual-only: rule errors passed via
 *     `errors` prop tint the offending task red. We don't re-run
 *     the backend validator client-side.
 */

import { useMemo } from "react"
import type { FormDAG } from "@/components/omnisight/dag-form-editor"
import type { DAGValidationError } from "@/lib/api"

// ─── layout ─────────────────────────────────────────────────────────

const NODE_W = 140
const NODE_H = 44
const GAP_X = 60 // between layers
const GAP_Y = 16 // between siblings in a layer
const PAD = 20

const TIER_COLOR: Record<string, string> = {
  t1: "var(--artifact-purple)",
  networked: "var(--neural-blue, #60a5fa)",
  t3: "var(--hardware-orange, #fb923c)",
}

interface Positioned {
  id: string
  x: number
  y: number
  layer: number
  tier: string
  description: string
}

/**
 * Compute the layer index for each task using longest-path-from-root:
 *   layer[n] = 1 + max(layer[d]) for d in depends_on
 *
 * Nodes whose deps don't resolve (forward references or unknown ids)
 * fall into layer 0 so the viewer still sees them; the validator
 * surfaces the actual error separately.
 */
function computeLayers(tasks: FormDAG["tasks"]): Map<string, number> {
  const layers = new Map<string, number>()
  const byId = new Map(tasks.map((t) => [t.task_id, t]))
  // Iterative relaxation: cap iterations to tasks.length to avoid
  // locking up on cycles (the validator will flag those; we just
  // don't want an infinite loop here).
  for (let pass = 0; pass < tasks.length + 1; pass++) {
    let changed = false
    for (const t of tasks) {
      const depLayers = t.depends_on
        .map((d) => layers.get(d))
        .filter((v): v is number => v !== undefined)
      const next = depLayers.length === t.depends_on.length && t.depends_on.every((d) => byId.has(d))
        ? (depLayers.length ? Math.max(...depLayers) + 1 : 0)
        : 0
      if (layers.get(t.task_id) !== next) {
        layers.set(t.task_id, next)
        changed = true
      }
    }
    if (!changed) break
  }
  return layers
}

function layout(dag: FormDAG): { nodes: Positioned[]; width: number; height: number } {
  const layers = computeLayers(dag.tasks)

  // Group tasks by layer, preserving list order within each layer.
  const byLayer = new Map<number, FormDAG["tasks"]>()
  for (const t of dag.tasks) {
    const l = layers.get(t.task_id) ?? 0
    if (!byLayer.has(l)) byLayer.set(l, [])
    byLayer.get(l)!.push(t)
  }

  const maxLayer = byLayer.size ? Math.max(...byLayer.keys()) : 0
  const maxStack = byLayer.size ? Math.max(...Array.from(byLayer.values(), (xs) => xs.length)) : 1

  const nodes: Positioned[] = []
  for (let L = 0; L <= maxLayer; L++) {
    const row = byLayer.get(L) ?? []
    // Centre the row vertically in the canvas.
    const rowHeight = row.length * NODE_H + (row.length - 1) * GAP_Y
    const totalHeight = maxStack * NODE_H + (maxStack - 1) * GAP_Y
    const yOffset = (totalHeight - rowHeight) / 2
    row.forEach((t, i) => {
      nodes.push({
        id: t.task_id,
        x: PAD + L * (NODE_W + GAP_X),
        y: PAD + yOffset + i * (NODE_H + GAP_Y),
        layer: L,
        tier: t.required_tier,
        description: t.description,
      })
    })
  }

  const width = PAD * 2 + (maxLayer + 1) * NODE_W + maxLayer * GAP_X
  const height = PAD * 2 + maxStack * NODE_H + (maxStack - 1) * GAP_Y
  return { nodes, width, height }
}

// ─── component ──────────────────────────────────────────────────────

interface Props {
  dag: FormDAG | null
  errors?: DAGValidationError[]
}

export function DagCanvas({ dag, errors = [] }: Props) {
  const { nodes, edges, width, height, redIds } = useMemo(() => {
    if (!dag || dag.tasks.length === 0) {
      return { nodes: [], edges: [], width: 320, height: 120, redIds: new Set<string>() }
    }
    const laid = layout(dag)
    const byId = new Map(laid.nodes.map((n) => [n.id, n]))
    const es: { from: string; to: string; d: string }[] = []
    for (const t of dag.tasks) {
      const child = byId.get(t.task_id)
      if (!child) continue
      for (const dep of t.depends_on) {
        const parent = byId.get(dep)
        if (!parent) continue
        // Edge: right edge of parent → left edge of child, bezier.
        const x1 = parent.x + NODE_W
        const y1 = parent.y + NODE_H / 2
        const x2 = child.x
        const y2 = child.y + NODE_H / 2
        const mx = (x1 + x2) / 2
        es.push({
          from: dep,
          to: t.task_id,
          d: `M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`,
        })
      }
    }
    // Every task id that appears in any validation error is tinted red.
    const reds = new Set<string>()
    for (const e of errors) if (e.task_id) reds.add(e.task_id)
    // Cycle errors are graph-level (task_id null); mark every node in a
    // dep chain so the user sees something is globally wrong.
    if (errors.some((e) => e.rule === "cycle" && !e.task_id)) {
      for (const n of laid.nodes) reds.add(n.id)
    }
    return { nodes: laid.nodes, edges: es, width: laid.width, height: laid.height, redIds: reds }
  }, [dag, errors])

  if (!dag || dag.tasks.length === 0) {
    return (
      <div className="text-xs font-mono text-[var(--muted-foreground)] text-center py-10 border border-dashed border-[var(--border)] rounded">
        Empty DAG — add a task to see the canvas.
      </div>
    )
  }

  return (
    <div
      className="rounded border border-[var(--border)] bg-[var(--background)] overflow-auto max-h-[480px]"
      aria-label="DAG canvas"
    >
      <svg
        width={width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label={`DAG ${dag.dag_id} — ${nodes.length} task${nodes.length === 1 ? "" : "s"}`}
        className="block"
      >
        {/* Arrow marker for edges */}
        <defs>
          <marker
            id="dag-arrow"
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="6"
            markerHeight="6"
            orient="auto-start-reverse"
          >
            <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--muted-foreground)" />
          </marker>
        </defs>

        {/* Edges first so nodes render over them */}
        {edges.map((e, i) => (
          <path
            key={i}
            d={e.d}
            stroke="var(--muted-foreground)"
            strokeWidth="1.2"
            fill="none"
            opacity="0.55"
            markerEnd="url(#dag-arrow)"
            data-from={e.from}
            data-to={e.to}
          />
        ))}

        {/* Nodes */}
        {nodes.map((n) => {
          const red = redIds.has(n.id)
          const stroke = red ? "var(--destructive)" : TIER_COLOR[n.tier] ?? "var(--border)"
          const handleFocus = () => {
            if (typeof window === "undefined") return
            // Bubble a focus request up — DagEditor listens, flips to
            // the Form tab, DagFormEditor scrolls/highlights the row.
            window.dispatchEvent(
              new CustomEvent("omnisight:dag-focus-task", {
                detail: { taskId: n.id },
              }),
            )
          }
          return (
            <g
              key={n.id}
              data-task-id={n.id}
              data-layer={n.layer}
              onClick={handleFocus}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault()
                  handleFocus()
                }
              }}
              role="button"
              tabIndex={0}
              aria-label={`Focus task ${n.id} in the Form editor`}
              className="cursor-pointer focus:outline-none focus-visible:[&>rect]:stroke-[var(--artifact-purple)]"
            >
              <rect
                x={n.x}
                y={n.y}
                width={NODE_W}
                height={NODE_H}
                rx="6"
                fill="var(--card)"
                stroke={stroke}
                strokeWidth={red ? 2 : 1.5}
              />
              <text
                x={n.x + 8}
                y={n.y + 16}
                fontSize="11"
                fontFamily="ui-monospace, monospace"
                fill="var(--foreground)"
                className="select-none"
              >
                {n.id.length > 18 ? n.id.slice(0, 16) + "…" : n.id}
              </text>
              <text
                x={n.x + 8}
                y={n.y + 32}
                fontSize="9"
                fontFamily="ui-monospace, monospace"
                fill={stroke}
                className="select-none"
              >
                {n.tier}
              </text>
              {/* Full description visible via browser tooltip */}
              <title>{n.description || n.id}</title>
            </g>
          )
        })}
      </svg>
    </div>
  )
}
