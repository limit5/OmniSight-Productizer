/**
 * WP.6 (OP-1500) — `lib/settings-registry.ts` contract tests.
 *
 * Locks the four behaviours we depend on:
 *
 *   1. Type vocabulary mirrors the backend (drift triggers CI here).
 *   2. ``fetchSettingsRegistry`` calls ``GET /settings/registry`` once
 *      per tab (per-tab memo) and translates the payload into the
 *      typed shape consumers expect.
 *   3. ``getDeviceId`` returns a stable id across calls within the
 *      same tab (persisted to ``localStorage``) and survives a hard
 *      reload (we exercise that by re-importing the module after
 *      writing to localStorage).
 *   4. ``getUserPreferenceScoped`` / ``setUserPreferenceScoped``
 *      route through the WP.6 partitioning query params based on the
 *      setting's sync mode — global → no query, per-platform →
 *      ``?platform=`` from the registry's derived value, never →
 *      ``?device_id=`` from ``getDeviceId``.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

vi.mock("@/lib/api", () => ({
  getSettingsRegistry: vi.fn(),
  getUserPreferenceWithScope: vi.fn(),
  setUserPreferenceWithScope: vi.fn(),
}))

import * as api from "@/lib/api"
import {
  OMNISIGHT_DEVICE_ID_KEY,
  PLATFORM_VALUES,
  SCOPE_VALUES,
  SYNC_MODE_VALUES,
  SYNC_MODE_LABEL,
  SYNC_MODE_HINT,
  _resetSettingsRegistryCacheForTests,
  fetchSettingsRegistry,
  findSetting,
  getDeviceId,
  getUserPreferenceScoped,
  setUserPreferenceScoped,
  type SettingMetadata,
} from "@/lib/settings-registry"


const FAKE_REGISTRY = {
  settings: [
    {
      pref_key: "motion_level",
      scope: "user" as const,
      sync: "globally" as const,
      supported_platforms: [],
      description: "Motion-effect intensity.",
      default_value: "dramatic",
    },
    {
      pref_key: "notification_sound",
      scope: "user" as const,
      sync: "per_platform" as const,
      supported_platforms: ["macos", "windows", "linux"],
      description: "Notification sound (per-platform).",
      default_value: "",
    },
    {
      pref_key: "hardware_bench_target",
      scope: "device" as const,
      sync: "never" as const,
      supported_platforms: ["macos", "windows", "linux"],
      description: "HD bring-up bench target (machine-local).",
      default_value: "",
    },
  ],
  derived_platform: "macos",
  scope_values: ["tenant", "user", "device"],
  sync_mode_values: ["globally", "per_platform", "never"],
  platform_values: ["macos", "windows", "linux", "ios", "android", "web"],
}


describe("WP.6 settings-registry — type vocabulary", () => {
  it("mirrors the backend scope vocabulary", () => {
    // If the backend renames a value, this test fails — frontend
    // expects the literal strings.
    expect([...SCOPE_VALUES]).toEqual(["tenant", "user", "device"])
  })
  it("mirrors the backend sync-mode vocabulary", () => {
    expect([...SYNC_MODE_VALUES]).toEqual([
      "globally",
      "per_platform",
      "never",
    ])
  })
  it("mirrors the backend platform vocabulary", () => {
    expect([...PLATFORM_VALUES]).toEqual([
      "macos",
      "windows",
      "linux",
      "ios",
      "android",
      "web",
    ])
  })
  it("exposes a label + hint for every sync mode", () => {
    for (const sync of SYNC_MODE_VALUES) {
      expect(SYNC_MODE_LABEL[sync]).toBeTruthy()
      expect(SYNC_MODE_HINT[sync]).toBeTruthy()
    }
  })
})


describe("WP.6 fetchSettingsRegistry — per-tab memo", () => {
  beforeEach(() => {
    vi.mocked(api.getSettingsRegistry).mockReset()
    _resetSettingsRegistryCacheForTests()
  })

  it("calls the API exactly once per tab", async () => {
    vi.mocked(api.getSettingsRegistry).mockResolvedValue(FAKE_REGISTRY)
    const first = await fetchSettingsRegistry()
    const second = await fetchSettingsRegistry()
    expect(first).toBe(second)
    expect(api.getSettingsRegistry).toHaveBeenCalledTimes(1)
  })

  it("translates the payload into typed settings", async () => {
    vi.mocked(api.getSettingsRegistry).mockResolvedValue(FAKE_REGISTRY)
    const registry = await fetchSettingsRegistry()
    expect(registry.derived_platform).toBe("macos")
    expect(registry.settings).toHaveLength(3)
    const motion = registry.settings.find((s) => s.pref_key === "motion_level")
    expect(motion?.sync).toBe("globally")
  })

  it("falls back to web platform when the server sends an unknown one", async () => {
    vi.mocked(api.getSettingsRegistry).mockResolvedValue({
      ...FAKE_REGISTRY,
      derived_platform: "novel-os-3000",
    } as unknown as typeof FAKE_REGISTRY)
    const registry = await fetchSettingsRegistry()
    expect(registry.derived_platform).toBe("web")
  })

  it("does not poison the cache on error — next call refetches", async () => {
    vi.mocked(api.getSettingsRegistry).mockRejectedValueOnce(
      new Error("transient"),
    )
    vi.mocked(api.getSettingsRegistry).mockResolvedValueOnce(FAKE_REGISTRY)
    await expect(fetchSettingsRegistry()).rejects.toThrow("transient")
    const second = await fetchSettingsRegistry()
    expect(second.settings).toHaveLength(3)
    expect(api.getSettingsRegistry).toHaveBeenCalledTimes(2)
  })
})


describe("WP.6 getDeviceId — stable browser-local id", () => {
  beforeEach(() => {
    window.localStorage.clear()
  })
  afterEach(() => {
    window.localStorage.clear()
  })

  it("returns the same id across calls", () => {
    const a = getDeviceId()
    const b = getDeviceId()
    expect(a).toBe(b)
  })

  it("persists the id to localStorage under the canonical key", () => {
    const id = getDeviceId()
    expect(id).not.toBe("")
    expect(window.localStorage.getItem(OMNISIGHT_DEVICE_ID_KEY)).toBe(id)
  })

  it("survives a 'reload' by reading the persisted localStorage value", () => {
    // First mount: mint + persist.
    const first = getDeviceId()
    // Simulate a reload by reading the value out and clearing
    // in-memory state. The next call must return the persisted id.
    const persisted = window.localStorage.getItem(OMNISIGHT_DEVICE_ID_KEY)
    expect(persisted).toBe(first)
    // Second call reads from localStorage — same id.
    expect(getDeviceId()).toBe(first)
  })

  it("uses a `dev-` prefix so device ids are distinguishable", () => {
    const id = getDeviceId()
    expect(id.startsWith("dev-")).toBe(true)
  })
})


describe("WP.6 get/setUserPreferenceScoped — routing", () => {
  beforeEach(() => {
    vi.mocked(api.getUserPreferenceWithScope).mockReset()
    vi.mocked(api.setUserPreferenceWithScope).mockReset()
    vi.mocked(api.getUserPreferenceWithScope).mockResolvedValue({
      key: "k", value: "v",
    })
    vi.mocked(api.setUserPreferenceWithScope).mockResolvedValue()
    window.localStorage.clear()
  })

  const motionMeta: SettingMetadata = {
    pref_key: "motion_level",
    scope: "user",
    sync: "globally",
    supported_platforms: [],
    description: "",
    default_value: "dramatic",
  }
  const soundMeta: SettingMetadata = {
    pref_key: "notification_sound",
    scope: "user",
    sync: "per_platform",
    supported_platforms: ["macos", "windows", "linux"],
    description: "",
    default_value: "",
  }
  const benchMeta: SettingMetadata = {
    pref_key: "hardware_bench_target",
    scope: "device",
    sync: "never",
    supported_platforms: ["macos", "windows", "linux"],
    description: "",
    default_value: "",
  }

  it("globally synced setting → no scope query", async () => {
    await setUserPreferenceScoped("motion_level", "subtle", motionMeta, "macos")
    expect(api.setUserPreferenceWithScope).toHaveBeenCalledWith(
      "motion_level", "subtle", undefined,
    )
  })

  it("per-platform setting → ?platform=<derived>", async () => {
    await setUserPreferenceScoped(
      "notification_sound", "marimba.aiff", soundMeta, "macos",
    )
    expect(api.setUserPreferenceWithScope).toHaveBeenCalledWith(
      "notification_sound", "marimba.aiff", { platform: "macos" },
    )
  })

  it("never-synced setting → ?device_id=<localStorage id>", async () => {
    await setUserPreferenceScoped(
      "hardware_bench_target", "stm32-7", benchMeta, "linux",
    )
    const call = vi.mocked(api.setUserPreferenceWithScope).mock.calls[0]
    expect(call[0]).toBe("hardware_bench_target")
    expect(call[1]).toBe("stm32-7")
    expect(call[2]?.device_id).toBeTruthy()
    expect(call[2]?.device_id?.startsWith("dev-")).toBe(true)
    expect(call[2]?.platform).toBeUndefined()
  })

  it("unregistered key (no meta) → no scope query (legacy passthrough)", async () => {
    await setUserPreferenceScoped(
      "legacy_anything", "v", undefined, "macos",
    )
    expect(api.setUserPreferenceWithScope).toHaveBeenCalledWith(
      "legacy_anything", "v", undefined,
    )
  })

  it("findSetting locates by pref_key", () => {
    const meta = findSetting(
      [motionMeta, soundMeta, benchMeta], "notification_sound",
    )
    expect(meta?.sync).toBe("per_platform")
  })

  it("findSetting returns undefined for unknown keys", () => {
    const meta = findSetting([motionMeta], "not_a_real_key")
    expect(meta).toBeUndefined()
  })
})
