"use client"

import { useEffect, useRef, useState } from "react"
import {
  subscribeEvents,
  type HostMetricsTickBaseline,
  type HostMetricsTickEvent,
  type HostMetricsTickSample,
  type SSEEvent,
} from "@/lib/api"

export const HOST_METRICS_HISTORY_SIZE = 60

export interface HostMetricsHistoryPoint {
  cpu_percent: number
  mem_percent: number
  mem_used_gb: number
  disk_percent: number
  loadavg_1m: number
  container_count: number
  sampled_at: number
}

export interface UseHostMetricsTickResult {
  latest: HostMetricsTickEvent | null
  history: HostMetricsHistoryPoint[]
  baseline: HostMetricsTickBaseline | null
  highPressure: boolean
  connected: boolean
}

function toPoint(ev: HostMetricsTickEvent): HostMetricsHistoryPoint {
  return {
    cpu_percent: ev.host.cpu_percent,
    mem_percent: ev.host.mem_percent,
    mem_used_gb: ev.host.mem_used_gb,
    disk_percent: ev.host.disk_percent,
    loadavg_1m: ev.host.loadavg_1m,
    container_count: ev.docker.container_count,
    sampled_at: ev.sampled_at,
  }
}

/**
 * Subscribe to the `host.metrics.tick` SSE stream.
 *
 * Exposes the freshest snapshot, a rolling 60-point history (for 5-minute
 * sparklines at the 5s tick cadence), the static baseline, and the
 * coordinator-computed `high_pressure` flag so panels can render live
 * pressure badges without a second round-trip to `/host/metrics`.
 */
export function useHostMetricsTick(): UseHostMetricsTickResult {
  const [latest, setLatest] = useState<HostMetricsTickEvent | null>(null)
  const [baseline, setBaseline] = useState<HostMetricsTickBaseline | null>(null)
  const [history, setHistory] = useState<HostMetricsHistoryPoint[]>([])
  const [connected, setConnected] = useState(false)
  const historyRef = useRef<HostMetricsHistoryPoint[]>([])

  useEffect(() => {
    const sub = subscribeEvents(
      (ev: SSEEvent) => {
        if (ev.event !== "host.metrics.tick") return
        const data = ev.data as HostMetricsTickEvent
        setLatest(data)
        setBaseline(data.baseline)
        const next = [...historyRef.current, toPoint(data)]
        if (next.length > HOST_METRICS_HISTORY_SIZE) {
          next.splice(0, next.length - HOST_METRICS_HISTORY_SIZE)
        }
        historyRef.current = next
        setHistory(next)
        setConnected(true)
      },
      () => setConnected(false),
    )
    return () => sub.close()
  }, [])

  return {
    latest,
    history,
    baseline,
    highPressure: latest?.high_pressure ?? false,
    connected,
  }
}

export type { HostMetricsTickEvent, HostMetricsTickSample, HostMetricsTickBaseline }
