/**
 * Phase 56-DAG-G — DagCanvas invariants.
 *
 * Validates the contract the canvas exposes to its parent:
 *   1. Empty DAG → placeholder, no svg.
 *   2. Tasks render as SVG nodes with data-task-id attributes.
 *   3. depends_on produces an edge; layer numbers follow longest-path.
 *   4. A validation error carrying task_id tints that node red.
 *   5. A graph-level cycle error tints every node red.
 */

import { describe, expect, it } from "vitest"
import { render } from "@testing-library/react"

import { DagCanvas } from "@/components/omnisight/dag-canvas"
import type { FormDAG } from "@/components/omnisight/dag-form-editor"

const twoTask: FormDAG = {
  schema_version: 1,
  dag_id: "REQ-t",
  tasks: [
    {
      task_id: "compile", description: "build",
      required_tier: "t1", toolchain: "cmake",
      expected_output: "build/a.bin", depends_on: [],
    },
    {
      task_id: "flash", description: "flash it",
      required_tier: "t3", toolchain: "flash_board",
      expected_output: "logs/f.log", depends_on: ["compile"],
    },
  ],
}

const fanOut: FormDAG = {
  schema_version: 1,
  dag_id: "REQ-fan",
  tasks: [
    { task_id: "root", description: "", required_tier: "t1", toolchain: "cmake", expected_output: "a", depends_on: [] },
    { task_id: "l1a", description: "", required_tier: "t1", toolchain: "x", expected_output: "b", depends_on: ["root"] },
    { task_id: "l1b", description: "", required_tier: "t1", toolchain: "x", expected_output: "c", depends_on: ["root"] },
    { task_id: "l2", description: "", required_tier: "t1", toolchain: "x", expected_output: "d", depends_on: ["l1a", "l1b"] },
  ],
}

describe("DagCanvas", () => {
  it("renders the empty-state placeholder when dag is null", () => {
    const { container, getByText } = render(<DagCanvas dag={null} />)
    expect(getByText(/empty dag/i)).toBeInTheDocument()
    expect(container.querySelector("svg")).toBeNull()
  })

  it("renders the empty-state placeholder when the task list is empty", () => {
    const empty: FormDAG = { schema_version: 1, dag_id: "REQ-x", tasks: [] }
    const { container, getByText } = render(<DagCanvas dag={empty} />)
    expect(getByText(/empty dag/i)).toBeInTheDocument()
    expect(container.querySelector("svg")).toBeNull()
  })

  it("emits one <g data-task-id> per task and one <path data-from> per edge", () => {
    const { container } = render(<DagCanvas dag={twoTask} />)
    const nodeGroups = container.querySelectorAll("g[data-task-id]")
    expect(nodeGroups).toHaveLength(2)
    const ids = Array.from(nodeGroups).map((g) => g.getAttribute("data-task-id"))
    expect(ids).toEqual(["compile", "flash"])

    const edges = container.querySelectorAll("path[data-from][data-to]")
    expect(edges).toHaveLength(1)
    expect(edges[0].getAttribute("data-from")).toBe("compile")
    expect(edges[0].getAttribute("data-to")).toBe("flash")
  })

  it("places tasks on their longest-path layer", () => {
    const { container } = render(<DagCanvas dag={fanOut} />)
    const byId = (id: string) => container.querySelector(`g[data-task-id="${id}"]`)
    expect(byId("root")?.getAttribute("data-layer")).toBe("0")
    expect(byId("l1a")?.getAttribute("data-layer")).toBe("1")
    expect(byId("l1b")?.getAttribute("data-layer")).toBe("1")
    // l2 depends on l1a AND l1b — longest path from root is 2.
    expect(byId("l2")?.getAttribute("data-layer")).toBe("2")
  })

  it("tints a task red when a validation error carries its task_id", () => {
    const { container } = render(
      <DagCanvas
        dag={twoTask}
        errors={[{ rule: "tier_violation", task_id: "flash", message: "denied" }]}
      />,
    )
    const flashRect = container
      .querySelector('g[data-task-id="flash"]')
      ?.querySelector("rect")
    const compileRect = container
      .querySelector('g[data-task-id="compile"]')
      ?.querySelector("rect")
    expect(flashRect?.getAttribute("stroke")).toContain("destructive")
    // Healthy node keeps its tier color (t1 = artifact-purple).
    expect(compileRect?.getAttribute("stroke")).not.toContain("destructive")
  })

  it("a graph-level cycle error tints every node red", () => {
    const { container } = render(
      <DagCanvas
        dag={twoTask}
        errors={[{ rule: "cycle", task_id: null, message: "A → B → A" }]}
      />,
    )
    const rects = container.querySelectorAll("g[data-task-id] rect")
    expect(rects).toHaveLength(2)
    for (const r of Array.from(rects)) {
      expect(r.getAttribute("stroke")).toContain("destructive")
    }
  })
})
