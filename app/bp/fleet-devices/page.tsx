"use client"

/**
 * OP-2307 (U4.5) — Productizer fleet "Devices" view.
 *
 * Lists every device the U4.4 fleet device-registry API exposes
 * (`GET /fleet/devices`) and, on drill-down, fetches that device's
 * apps.manifest (`GET /fleet/devices/{id}/manifest`) and renders the
 * launcher tile grid via the vendored launcher-web `AppGrid` (U4.3).
 * This makes the productizer the 4th consumer of the one shared UI —
 * a read-only remote mirror of what each device shows locally.
 *
 * MUST NOT: do NOT remote-drive / dispatch on tile activate (U4.6).
 * Do NOT reimplement the launcher components. Do NOT do a live device
 * fetch — the API is fixture-backed.
 */

import { useCallback, useEffect, useState } from "react"
import Link from "next/link"
import { useRouter } from "next/navigation"
import {
  ArrowLeft,
  ChevronRight,
  Loader2,
  RefreshCw,
  Server,
} from "lucide-react"

import { useAuth } from "@/lib/auth-context"
import {
  getFleetDevices,
  getFleetDeviceManifest,
  type FleetDevice,
  type FleetDeviceManifest,
} from "@/lib/api"
import { DeviceLauncher } from "@/components/omnisight/device-launcher"

export default function FleetDevicesPage() {
  const auth = useAuth()
  const router = useRouter()
  const [devices, setDevices] = useState<FleetDevice[] | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [manifest, setManifest] = useState<FleetDeviceManifest | null>(null)
  const [manifestLoading, setManifestLoading] = useState(false)
  const [manifestError, setManifestError] = useState<string | null>(null)

  useEffect(() => {
    if (auth.loading) return
    if (!auth.user && auth.authMode !== "open") {
      const next =
        typeof window !== "undefined"
          ? window.location.pathname + window.location.search
          : "/bp/fleet-devices"
      router.replace(`/login?next=${encodeURIComponent(next)}`)
    }
  }, [auth.loading, auth.user, auth.authMode, router])

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setDevices(await getFleetDevices())
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
      setDevices(null)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (auth.loading) return
    if (!auth.user && auth.authMode !== "open") return
    const timer = window.setTimeout(() => {
      void refresh()
    }, 0)
    return () => window.clearTimeout(timer)
  }, [auth.loading, auth.user, auth.authMode, refresh])

  const handleSelect = useCallback(async (id: string) => {
    setSelectedId(id)
    setManifest(null)
    setManifestError(null)
    setManifestLoading(true)
    try {
      setManifest(await getFleetDeviceManifest(id))
    } catch (exc) {
      setManifestError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setManifestLoading(false)
    }
  }, [])

  const handleBack = useCallback(() => {
    setSelectedId(null)
    setManifest(null)
    setManifestError(null)
  }, [])

  if (auth.loading || (!auth.user && auth.authMode !== "open")) {
    return (
      <main className="min-h-screen flex items-center justify-center bg-[var(--background)] text-[var(--foreground)]">
        <div className="font-mono text-xs text-[var(--muted-foreground)] flex items-center gap-2">
          <Loader2 size={14} className="animate-spin" />
          Verifying operator session...
        </div>
      </main>
    )
  }

  const selectedDevice =
    selectedId && devices
      ? devices.find((d) => d.id === selectedId) ?? null
      : null

  return (
    <main
      className="min-h-screen bg-[var(--background)] text-[var(--foreground)] p-6 md:p-10"
      data-testid="fleet-devices-page"
    >
      <div className="max-w-6xl mx-auto">
        <header className="flex items-start justify-between gap-4 mb-6 flex-wrap">
          <div>
            <div className="flex items-center gap-2 text-[10px] font-mono text-[var(--muted-foreground)] mb-1">
              <Link
                href="/"
                className="hover:text-[var(--foreground)] inline-flex items-center gap-1"
              >
                <ArrowLeft size={10} /> dashboard
              </Link>
              <ChevronRight size={10} />
              <span className={selectedDevice ? undefined : "text-[var(--foreground)]"}>
                {selectedDevice ? (
                  <button
                    type="button"
                    onClick={handleBack}
                    className="hover:text-[var(--foreground)] inline-flex items-center gap-1"
                    data-testid="fleet-devices-breadcrumb-back"
                  >
                    bp / fleet-devices
                  </button>
                ) : (
                  "bp / fleet-devices"
                )}
              </span>
              {selectedDevice && (
                <>
                  <ChevronRight size={10} />
                  <span className="text-[var(--foreground)]">
                    {selectedDevice.name}
                  </span>
                </>
              )}
            </div>
            <h1 className="text-xl font-semibold flex items-center gap-2">
              <Server size={20} />
              Fleet Devices
            </h1>
            <p className="text-xs text-[var(--muted-foreground)] mt-1">
              Read-only mirror of every fleet device&apos;s launcher.
              Drill into a device to see the same app-tile grid it renders
              locally — the productizer is the 4th consumer of the shared
              launcher-web components.
            </p>
          </div>
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={loading}
            className="inline-flex items-center gap-1 px-3 py-1.5 rounded border border-[var(--border)] bg-[var(--card)] hover:bg-[var(--secondary)]/40 text-xs font-mono disabled:opacity-50"
            data-testid="fleet-devices-refresh"
          >
            <RefreshCw size={12} className={loading ? "animate-spin" : ""} />
            Refresh
          </button>
        </header>

        {error && (
          <div
            className="mb-4 rounded border border-[var(--destructive)]/40 bg-[var(--destructive)]/10 p-3 text-xs font-mono text-[var(--destructive)]"
            data-testid="fleet-devices-error"
          >
            Failed to load fleet devices: {error}
          </div>
        )}

        {selectedDevice ? (
          <DeviceDetail
            device={selectedDevice}
            manifest={manifest}
            loading={manifestLoading}
            error={manifestError}
            onBack={handleBack}
          />
        ) : (
          <DeviceList
            devices={devices}
            loading={loading}
            onSelect={handleSelect}
          />
        )}
      </div>
    </main>
  )
}

interface DeviceListProps {
  devices: FleetDevice[] | null
  loading: boolean
  onSelect: (id: string) => void
}

function DeviceList({ devices, loading, onSelect }: DeviceListProps) {
  if (loading && !devices) {
    return (
      <div
        className="rounded-md border border-dashed bg-muted/20 p-6 text-center text-xs font-mono text-muted-foreground"
        data-testid="fleet-devices-loading"
      >
        <Loader2 size={14} className="animate-spin inline-block mr-2" />
        Loading fleet devices...
      </div>
    )
  }
  if (!devices || devices.length === 0) {
    return (
      <div
        className="rounded-md border border-[var(--border)] bg-[var(--card)] p-6 text-center text-xs font-mono text-[var(--muted-foreground)]"
        data-testid="fleet-devices-empty"
      >
        No fleet devices registered.
      </div>
    )
  }
  return (
    <ul
      className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3"
      data-testid="fleet-devices-list"
    >
      {devices.map((device) => (
        <li key={device.id}>
          <button
            type="button"
            onClick={() => onSelect(device.id)}
            data-testid={`fleet-device-card-${device.id}`}
            data-device-id={device.id}
            className="w-full text-left rounded border border-[var(--border)] bg-[var(--card)] hover:bg-[var(--secondary)]/40 p-4 transition-colors"
          >
            <div className="flex items-center justify-between gap-2 mb-1">
              <span className="font-semibold text-sm">{device.name}</span>
              <span className="text-[10px] font-mono text-[var(--muted-foreground)] uppercase">
                {device.renderer}
              </span>
            </div>
            <div className="text-[11px] font-mono text-[var(--muted-foreground)] flex items-center gap-2 flex-wrap">
              <span>{device.id}</span>
              <span>·</span>
              <span>{device.model}</span>
              {device.last_seen && (
                <>
                  <span>·</span>
                  <span>seen {device.last_seen}</span>
                </>
              )}
            </div>
          </button>
        </li>
      ))}
    </ul>
  )
}

interface DeviceDetailProps {
  device: FleetDevice
  manifest: FleetDeviceManifest | null
  loading: boolean
  error: string | null
  onBack: () => void
}

function DeviceDetail({
  device,
  manifest,
  loading,
  error,
  onBack,
}: DeviceDetailProps) {
  return (
    <section
      className="space-y-4"
      data-testid={`fleet-device-detail-${device.id}`}
    >
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div>
          <h2 className="text-lg font-semibold">{device.name}</h2>
          <p className="text-[11px] font-mono text-[var(--muted-foreground)]">
            {device.id} · {device.model} · renderer={device.renderer}
            {device.last_seen ? ` · seen ${device.last_seen}` : ""}
          </p>
        </div>
        <button
          type="button"
          onClick={onBack}
          className="inline-flex items-center gap-1 px-3 py-1.5 rounded border border-[var(--border)] bg-[var(--card)] hover:bg-[var(--secondary)]/40 text-xs font-mono"
          data-testid="fleet-device-back"
        >
          <ArrowLeft size={12} /> All devices
        </button>
      </div>

      {error && (
        <div
          className="rounded border border-[var(--destructive)]/40 bg-[var(--destructive)]/10 p-3 text-xs font-mono text-[var(--destructive)]"
          data-testid="fleet-device-manifest-error"
        >
          Failed to load manifest: {error}
        </div>
      )}

      {loading && !manifest ? (
        <div
          className="rounded-md border border-dashed bg-muted/20 p-6 text-center text-xs font-mono text-muted-foreground"
          data-testid="fleet-device-manifest-loading"
        >
          <Loader2 size={14} className="animate-spin inline-block mr-2" />
          Loading manifest...
        </div>
      ) : manifest ? (
        <DeviceLauncher manifest={manifest} />
      ) : null}
    </section>
  )
}
