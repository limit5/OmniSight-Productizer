import ConflictTrendTile from "@/components/admin/conflict-trend-tile"

export default function Page() {
  return (
    <main
      className="min-h-screen bg-[var(--background)] text-[var(--foreground)] p-6 md:p-10"
      data-testid="conflict-trend-page"
    >
      <div className="max-w-3xl mx-auto space-y-4">
        <header>
          <h1 className="text-xl font-semibold">Conflict trend</h1>
          <p className="text-xs text-[var(--muted-foreground)] mt-1">
            24h + 7d roll-ups of the conflict-observations table. Daily
            JSON report lands at{" "}
            <code>/var/log/omnisight/conflict-report-&lt;date&gt;.json</code>.
          </p>
        </header>
        <ConflictTrendTile pollIntervalMs={60_000} />
      </div>
    </main>
  )
}
