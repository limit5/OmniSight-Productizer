"use client"

/**
 * OP-949 H4 — ``/admin/release-approvals`` admin page.
 *
 * Thin wrapper around ``ReleaseApprovalsPanel``. The panel owns every
 * piece of behaviour (fetching, SSE re-subscribe, optimistic-locking
 * handshake); this page only handles the page-chrome breadcrumbs and
 * the ``onAuthRefused`` → ``/login`` redirect that the panel cannot do
 * by itself (a leaf component should not call ``router.push``).
 */

import { useCallback } from "react"
import Link from "next/link"
import { useRouter } from "next/navigation"
import { ArrowLeft, ChevronRight, ShieldCheck } from "lucide-react"

import { ReleaseApprovalsPanel } from "@/components/omnisight/admin/ReleaseApprovalsPanel"

export default function AdminReleaseApprovalsPage() {
  const router = useRouter()
  const onAuthRefused = useCallback(() => {
    const next =
      typeof window !== "undefined"
        ? `?next=${encodeURIComponent(window.location.pathname)}`
        : ""
    router.push(`/login${next}`)
  }, [router])

  return (
    <main
      className="min-h-screen bg-[var(--background)] text-[var(--foreground)] p-6 md:p-10"
      data-testid="admin-release-approvals-page"
    >
      <div className="max-w-6xl mx-auto">
        <header className="flex items-center justify-between mb-6">
          <div>
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
              Release Approvals
            </h1>
            <p className="text-xs text-[var(--muted-foreground)] mt-1">
              Operator gate replacing the JIRA +2 approval. Lists every
              release sitting in <code className="font-mono">pending_approval</code>{" "}
              and every approved release still in canary 5% (still abortable).
            </p>
          </div>
        </header>

        <ReleaseApprovalsPanel onAuthRefused={onAuthRefused} />
      </div>
    </main>
  )
}
