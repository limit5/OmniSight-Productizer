"use client"

/**
 * OP-949 H4 — Operator approval admin page.
 *
 * Mounts the `ReleaseApprovalsPanel` for the `/admin/release-approvals`
 * route. The panel owns its own data + SSE + race-protection wiring;
 * this file is the route shell only.
 */

import Link from "next/link"
import { ArrowLeft, ChevronRight, ShieldCheck } from "lucide-react"

import { ReleaseApprovalsPanel } from "@/components/omnisight/admin/ReleaseApprovalsPanel"

export default function AdminReleaseApprovalsPage() {
  return (
    <main
      className="min-h-screen bg-[var(--background)] text-[var(--foreground)] p-6 md:p-10"
      data-testid="admin-release-approvals-page"
    >
      <div className="max-w-6xl mx-auto">
        <header className="mb-6">
          <div className="flex items-center gap-2 text-[10px] font-mono text-[var(--muted-foreground)] mb-1">
            <Link
              href="/"
              className="hover:text-[var(--foreground)] inline-flex items-center gap-1"
            >
              <ArrowLeft size={10} /> dashboard
            </Link>
            <ChevronRight size={10} />
            <span>admin</span>
            <ChevronRight size={10} />
            <span className="text-[var(--foreground)]">release approvals</span>
          </div>
          <h1 className="text-xl font-semibold flex items-center gap-2">
            <ShieldCheck size={20} />
            Release approvals
          </h1>
          <p className="text-xs text-[var(--muted-foreground)] mt-1">
            Operator approval gate for in-flight releases. Replaces the
            legacy JIRA +2 sign-off per L1 R8 / ADR-0018. Only the
            non-ai-reviewer human group may approve or abort.
          </p>
        </header>
        <ReleaseApprovalsPanel />
      </div>
    </main>
  )
}
