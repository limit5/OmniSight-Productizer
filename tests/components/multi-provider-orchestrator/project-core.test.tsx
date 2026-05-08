import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import ProjectCore from "@/components/omnisight/multi-provider-orchestrator/ProjectCore"
import type { ProviderConstellationTaskSummary } from "@/components/omnisight/multi-provider-orchestrator/ProviderConstellation"

describe("<ProjectCore>", () => {
  it("renders the task counts and circular brand tile", () => {
    const taskSummary = {
      title: "Project Core",
      taskCount: 7,
      activeCount: 7,
      queuedCount: 3,
      estimatedTokens: 42_000,
    } as ProviderConstellationTaskSummary

    const { container } = render(<ProjectCore taskSummary={taskSummary} />)

    expect(screen.getByTestId("project-core-active-count")).toHaveTextContent(
      "7",
    )
    expect(screen.getByTestId("project-core-queued-count")).toHaveTextContent(
      "3",
    )
    expect(screen.getByTestId("project-core-brand-icon")).toBeInTheDocument()
    expect(screen.getByTestId("project-core")).toHaveClass("rounded-full")
    expect(container.firstChild).toMatchInlineSnapshot(`
      <section
        aria-label="Project Core task summary"
        class="flex h-40 w-40 flex-col items-center justify-center rounded-full border border-[var(--neural-cyan,#67e8f9)]/65 bg-[var(--background,#020617)]/80 p-4 text-center shadow-[0_0_48px_rgba(103,232,249,0.22)] sm:h-48 sm:w-48"
        data-testid="project-core"
      >
        <div
          class="flex size-10 items-center justify-center rounded-full border border-[var(--neural-cyan,#67e8f9)]/35 bg-[var(--neural-cyan,#67e8f9)]/10"
          data-testid="project-core-brand-icon"
        >
          <svg
            aria-hidden="true"
            class="lucide lucide-network size-5 text-[var(--neural-cyan,#67e8f9)]"
            fill="none"
            height="24"
            stroke="currentColor"
            stroke-linecap="round"
            stroke-linejoin="round"
            stroke-width="2"
            viewBox="0 0 24 24"
            width="24"
            xmlns="http://www.w3.org/2000/svg"
          >
            <rect
              height="6"
              rx="1"
              width="6"
              x="16"
              y="16"
            />
            <rect
              height="6"
              rx="1"
              width="6"
              x="2"
              y="16"
            />
            <rect
              height="6"
              rx="1"
              width="6"
              x="9"
              y="2"
            />
            <path
              d="M5 16v-3a1 1 0 0 1 1-1h12a1 1 0 0 1 1 1v3"
            />
            <path
              d="M12 12V8"
            />
          </svg>
        </div>
        <h3
          class="mt-3 max-w-full truncate text-sm font-semibold text-[var(--foreground,#e2e8f0)]"
        >
          Project Core
        </h3>
        <dl
          class="mt-3 grid w-full grid-cols-2 gap-2 font-mono text-[10px] uppercase text-[var(--muted-foreground,#94a3b8)]"
        >
          <div
            class="min-w-0 rounded-sm border border-white/10 bg-white/[0.03] px-2 py-1"
          >
            <dt>
              Active
            </dt>
            <dd
              class="text-sm font-semibold text-[var(--neural-cyan,#67e8f9)]"
              data-testid="project-core-active-count"
            >
              7
            </dd>
          </div>
          <div
            class="min-w-0 rounded-sm border border-white/10 bg-white/[0.03] px-2 py-1"
          >
            <dt>
              Queued
            </dt>
            <dd
              class="text-sm font-semibold text-[var(--foreground,#e2e8f0)]"
              data-testid="project-core-queued-count"
            >
              3
            </dd>
          </div>
        </dl>
      </section>
    `)
  })
})
