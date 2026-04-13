/**
 * Slash Command Registry — shared definition for all / commands.
 *
 * Commands are categorized and can be handled:
 * - "local": Frontend-only, no network call
 * - "api": Frontend calls a REST API directly
 * - "backend": Sent to /chat for backend processing
 */

export interface SlashCommand {
  name: string
  description: string
  category: "system" | "dev" | "hardware" | "agent" | "provider" | "npi" | "tools"
  args?: string          // e.g., "[module]", "[type]", "[provider] [model]"
  handler: "local" | "api" | "backend"
}

export const SLASH_COMMANDS: SlashCommand[] = [
  // ── System ──
  { name: "status",    description: "系統總覽（agents/tasks/memory）",        category: "system",   handler: "api" },
  { name: "info",      description: "主機資訊（CPU/RAM/Disk/Kernel）",        category: "system",   handler: "api" },
  { name: "debug",     description: "除錯狀態（errors/blocked/findings）",    category: "system",   handler: "api" },
  { name: "logs",      description: "最近系統日誌",                           category: "system",   args: "[limit]", handler: "api" },
  { name: "devices",   description: "USB/儲存/網路裝置",                     category: "system",   handler: "api" },

  // ── Development ──
  { name: "build",     description: "觸發交叉編譯",                          category: "dev",      args: "[module]", handler: "backend" },
  { name: "test",      description: "執行測試",                              category: "dev",      args: "[module]", handler: "backend" },
  { name: "simulate",  description: "執行雙軌模��驗證",                      category: "dev",      args: "[module]", handler: "backend" },
  { name: "review",    description: "觸發 Gerrit 程式碼審查",               category: "dev",      handler: "backend" },
  { name: "platform",  description: "顯示 platform 編譯參數",               category: "dev",      args: "[name]", handler: "backend" },

  // ── Hardware ──
  { name: "deploy",    description: "部署到 EVK 開發板",                       category: "hardware", args: "[platform] [module]", handler: "backend" },
  { name: "evk",       description: "檢查 EVK 連線狀態",                      category: "hardware", args: "[platform]", handler: "backend" },
  { name: "stream",    description: "列出 UVC 攝影機裝置",                     category: "hardware", handler: "backend" },

  // ── Agent ──
  { name: "spawn",     description: "建立新 Agent",                         category: "agent",    args: "[type]", handler: "api" },
  { name: "agents",    description: "列出所有 Agent 狀態",                  category: "agent",    handler: "api" },
  { name: "tasks",     description: "列出所有 Task 狀態",                   category: "agent",    handler: "api" },
  { name: "assign",    description: "分派任務給 Agent",                     category: "agent",    args: "[task] [agent]", handler: "backend" },
  { name: "invoke",    description: "觸發 INVOKE 全局調度",                 category: "agent",    args: "[command]", handler: "local" },

  // ── Provider ──
  { name: "provider",  description: "LLM Provider 狀態與健康度",            category: "provider", handler: "api" },
  { name: "switch",    description: "切換 LLM Provider",                    category: "provider", args: "[provider] [model]", handler: "api" },
  { name: "budget",    description: "Token 預算狀態",                       category: "provider", handler: "api" },

  // ── NPI ──
  { name: "npi",       description: "NPI 生命週期狀態",                     category: "npi",      handler: "api" },
  { name: "sdks",      description: "Vendor SDK 狀態",                      category: "npi",      handler: "api" },

  // ── Tools ���─
  { name: "help",      description: "顯示所有快速指令",                     category: "tools",    handler: "local" },
  { name: "clear",     description: "清除聊天記錄",                         category: "tools",    handler: "local" },
  { name: "refresh",   description: "強制刷新系統資料",                     category: "tools",    handler: "local" },
]

export function matchCommands(input: string): SlashCommand[] {
  if (!input.startsWith("/")) return []
  const query = input.slice(1).toLowerCase()
  if (!query) return SLASH_COMMANDS
  return SLASH_COMMANDS.filter(c => c.name.startsWith(query))
}

export function parseSlashCommand(input: string): { name: string; args: string } | null {
  if (!input.startsWith("/")) return null
  const parts = input.slice(1).trim().split(/\s+/)
  const name = parts[0]?.toLowerCase() || ""
  const cmd = SLASH_COMMANDS.find(c => c.name === name)
  if (!cmd) return null
  return { name, args: parts.slice(1).join(" ") }
}

export const CATEGORY_LABELS: Record<string, string> = {
  system: "SYSTEM",
  dev: "DEV",
  hardware: "HARDWARE",
  agent: "AGENT",
  provider: "PROVIDER",
  npi: "NPI",
  tools: "TOOLS",
}

export const CATEGORY_COLORS: Record<string, string> = {
  system: "var(--neural-blue)",
  dev: "var(--hardware-orange)",
  hardware: "var(--critical-red)",
  agent: "var(--validation-emerald)",
  provider: "var(--artifact-purple)",
  npi: "var(--neural-blue)",
  tools: "var(--muted-foreground)",
}
