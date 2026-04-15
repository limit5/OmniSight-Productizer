/**
 * B8 — DAG toolchain enum / autocomplete integration tests.
 */

import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, screen, waitFor } from "@testing-library/react"

// ─── Mock API (vi.mock is hoisted — no external refs) ────────

vi.mock("@/lib/api", () => ({
  validateDag: vi.fn().mockResolvedValue({
    ok: true,
    stage: "semantic",
    errors: [],
    warnings: [
      {
        rule: "unknown_toolchain",
        task_id: "build",
        message: "toolchain 'typo' is not in the known toolchain registry",
      },
    ],
  }),
  submitDag: vi.fn(),
  fetchToolchains: vi.fn().mockResolvedValue({
    all: ["cmake", "gcc", "flash_board", "aarch64-linux-gnu-gcc"],
    by_platform: { host_native: "gcc", aarch64: "aarch64-linux-gnu-gcc" },
    by_tier: { t1: ["cmake", "gcc"], t3: ["flash_board"] },
  }),
}))

import {
  DagFormEditor,
  type FormDAG,
} from "@/components/omnisight/dag-form-editor"

const sample: FormDAG = {
  schema_version: 1,
  dag_id: "REQ-tc-test",
  tasks: [
    {
      task_id: "build",
      description: "compile",
      required_tier: "t1",
      toolchain: "cmake",
      expected_output: "build/out.bin",
      depends_on: [],
    },
  ],
}

describe("B8: Toolchain enum / autocomplete", () => {
  beforeEach(() => vi.clearAllMocks())

  it("renders a datalist with known toolchains after fetch", async () => {
    const onChange = vi.fn()
    render(<DagFormEditor value={sample} onChange={onChange} />)

    await waitFor(() => {
      const datalist = document.getElementById(
        "omnisight-toolchains",
      ) as HTMLDataListElement
      expect(datalist).toBeTruthy()
      const options = datalist.querySelectorAll("option")
      expect(options.length).toBe(4)
      const values = Array.from(options).map((o) => o.value)
      expect(values).toEqual(
        expect.arrayContaining(["cmake", "gcc", "flash_board"]),
      )
    })
  })

  it("toolchain input references the datalist via list attribute", async () => {
    const onChange = vi.fn()
    render(<DagFormEditor value={sample} onChange={onChange} />)
    await waitFor(() => {
      expect(
        document.getElementById("omnisight-toolchains"),
      ).toBeTruthy()
    })
    const input = screen.getByLabelText("task 1 toolchain")
    expect(input.getAttribute("list")).toBe("omnisight-toolchains")
  })
})
