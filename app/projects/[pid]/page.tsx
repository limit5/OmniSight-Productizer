"use client"

/**
 * OP-1785 - project progress page.
 *
 * Mirrors the project settings route's tenant/project resolution, but
 * renders a read-only lifecycle view instead of management controls.
 */

import { use, useMemo } from "react"
import Link from "next/link"
import {
  ArrowLeft,
  ChevronRight,
  CircleAlert,
  Folder,
  Loader2,
  Settings,
} from "lucide-react"
import { ProjectLifecycleProgress } from "@/components/omnisight/project-lifecycle-progress"
import { ProjectDecisionInbox } from "@/components/omnisight/project-decision-inbox"
import { useTenant } from "@/lib/tenant-context"
import { useProject } from "@/lib/project-context"

const PROJECT_ID_PATTERN = /^p-[a-z0-9][a-z0-9-]{2,63}$/

export default function ProjectProgressPage({
  params,
}: {
  params: Promise<{ pid: string }>
}) {
  const { pid } = use(params)
  const { currentTenantId } = useTenant()
  const { projects, loading: projectsLoading } = useProject()

  const projectIdValid = useMemo(() => PROJECT_ID_PATTERN.test(pid), [pid])
  const project = useMemo(
    () => projects.find((p) => p.project_id === pid) ?? null,
    [projects, pid],
  )

  if (!projectIdValid) {
    return (
      <main
        className="flex min-h-screen items-center justify-center bg-[var(--background)] p-6 text-[var(--foreground)]"
        data-testid="project-progress-bad-id"
      >
        <div className="w-full max-w-md rounded border border-[var(--destructive)]/40 bg-[var(--card)] p-6 font-mono">
          <div className="mb-2 flex items-center gap-2 text-[var(--destructive)]">
            <CircleAlert size={16} />
            <span className="text-sm font-semibold">Invalid project id</span>
          </div>
          <p className="mb-4 text-xs leading-relaxed text-[var(--muted-foreground)]">
            <code className="rounded bg-[var(--secondary)]/40 px-1">{pid}</code>{" "}
            does not match the project id pattern.
          </p>
          <Link
            href="/"
            className="inline-flex items-center gap-1 text-xs text-[var(--neural-blue)] underline"
          >
            <ArrowLeft size={12} /> Back to dashboard
          </Link>
        </div>
      </main>
    )
  }

  if (projectsLoading) {
    return (
      <main className="flex min-h-screen items-center justify-center bg-[var(--background)] text-[var(--foreground)]">
        <div className="flex items-center gap-2 font-mono text-xs text-[var(--muted-foreground)]" data-testid="project-progress-loading">
          <Loader2 size={14} className="animate-spin" />
          Loading project...
        </div>
      </main>
    )
  }

  if (!currentTenantId) {
    return (
      <main
        className="flex min-h-screen items-center justify-center bg-[var(--background)] p-6 text-[var(--foreground)]"
        data-testid="project-progress-no-tenant"
      >
        <div className="w-full max-w-md rounded border border-[var(--border)] bg-[var(--card)] p-6 font-mono">
          <div className="mb-2 text-sm font-semibold">Select a tenant first</div>
          <p className="mb-4 text-xs leading-relaxed text-[var(--muted-foreground)]">
            Project progress is tenant scoped. Use the tenant dropdown
            in the dashboard header to pick a tenant, then navigate back here.
          </p>
          <Link
            href="/"
            className="inline-flex items-center gap-1 text-xs text-[var(--neural-blue)] underline"
          >
            <ArrowLeft size={12} /> Back to dashboard
          </Link>
        </div>
      </main>
    )
  }

  if (!project) {
    return (
      <main
        className="flex min-h-screen items-center justify-center bg-[var(--background)] p-6 text-[var(--foreground)]"
        data-testid="project-progress-not-found"
      >
        <div className="w-full max-w-md rounded border border-[var(--destructive)]/40 bg-[var(--card)] p-6 font-mono">
          <div className="mb-2 flex items-center gap-2 text-[var(--destructive)]">
            <CircleAlert size={16} />
            <span className="text-sm font-semibold">Project not found in current tenant</span>
          </div>
          <p className="mb-4 text-xs leading-relaxed text-[var(--muted-foreground)]">
            <code className="rounded bg-[var(--secondary)]/40 px-1">{pid}</code>{" "}
            is not a project on tenant{" "}
            <code className="rounded bg-[var(--secondary)]/40 px-1">{currentTenantId}</code>.
            Switch to the project&apos;s tenant via the dashboard header dropdown and try again.
          </p>
          <Link
            href="/"
            className="inline-flex items-center gap-1 text-xs text-[var(--neural-blue)] underline"
          >
            <ArrowLeft size={12} /> Back to dashboard
          </Link>
        </div>
      </main>
    )
  }

  return (
    <main
      className="min-h-screen bg-[var(--background)] p-6 text-[var(--foreground)] md:p-10"
      data-testid="project-progress-page"
    >
      <div className="mx-auto max-w-6xl">
        <header className="mb-6">
          <div className="mb-1 flex items-center gap-2 font-mono text-[10px] text-[var(--muted-foreground)]">
            <Link href="/" className="inline-flex items-center gap-1 hover:text-[var(--foreground)]">
              <ArrowLeft size={10} /> dashboard
            </Link>
            <ChevronRight size={10} />
            <span>tenants</span>
            <ChevronRight size={10} />
            <span>{project.tenant_id}</span>
            <ChevronRight size={10} />
            <span>projects</span>
            <ChevronRight size={10} />
            <span className="text-[var(--foreground)]">{project.slug}</span>
            <ChevronRight size={10} />
            <span className="text-[var(--foreground)]">progress</span>
          </div>
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h1 className="flex items-center gap-2 text-xl font-semibold">
                <Folder size={20} />
                Project progress / <span className="font-mono text-base">{project.name}</span>
              </h1>
              <p className="mt-1 text-xs text-[var(--muted-foreground)]">
                One project-scoped view of specification, build, test, and delivery signals.
              </p>
            </div>
            <Link
              href={`/projects/${encodeURIComponent(pid)}/settings`}
              className="inline-flex items-center gap-1 rounded border border-[var(--border)] bg-[var(--card)] px-3 py-1.5 font-mono text-xs hover:bg-[var(--secondary)]/40"
              data-testid="project-progress-settings-link"
            >
              <Settings size={12} />
              Settings
            </Link>
          </div>
        </header>

        <div className="flex flex-col gap-6">
          <ProjectLifecycleProgress projectId={pid} />
          <ProjectDecisionInbox projectId={pid} />
        </div>
      </div>
    </main>
  )
}
