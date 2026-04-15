"use client"

import { createContext, useContext, useState, useEffect, type ReactNode } from "react"

export type Locale = "en" | "zh-CN" | "zh-TW" | "ja"

interface I18nContextType {
  locale: Locale
  setLocale: (locale: Locale) => void
  t: (key: string, params?: Record<string, string | number>) => string
}

const I18nContext = createContext<I18nContextType | null>(null)

// Translation dictionaries
const translations: Record<Locale, Record<string, string>> = {
  en: {
    // Header
    "header.title": "OMNISIGHT PRODUCTIZER",
    "header.subtitle": "NEURAL COMMAND CENTER v2.0",
    "header.pipeline": "PROJECT PIPELINE",
    "header.complete": "COMPLETE",
    
    // Panels
    "panel.host": "Host & Devices",
    "panel.spec": "Spec Matrix",
    "panel.agents": "Agent Matrix",
    "panel.orchestrator": "Orchestrator AI",
    "panel.tasks": "Task Backlog",
    "panel.source": "Source Control",
    "panel.vitals": "Vitals & Artifacts",
    
    // Orchestrator
    "orchestrator.title": "ORCHESTRATOR AI",
    "orchestrator.subtitle": "CENTRAL COORDINATOR",
    "orchestrator.agents": "AGENTS",
    "orchestrator.suggestions": "AI SUGGESTIONS",
    "orchestrator.placeholder": "Type command or message...",
    "orchestrator.accept": "Accept",
    "orchestrator.reject": "Reject",
    "orchestrator.reassign": "Reassign",
    
    // Status
    "status.idle": "Idle",
    "status.running": "Running",
    "status.completed": "Completed",
    "status.error": "Error",
    "status.pending": "Pending",
    "status.synced": "SYNCED",
    
    // Actions
    "action.spawn": "Spawn",
    "action.assign": "Assign",
    "action.stop": "HALT",
    "action.resume": "RESUME",
    
    // Common
    "common.loading": "Loading...",
    "common.error": "Error",
    "common.success": "Success",
    "common.cancel": "Cancel",
    "common.confirm": "Confirm",
    "common.save": "Save",
    "common.delete": "Delete",
    "common.add": "Add",
    "common.remove": "Remove",
  },
  
  "zh-CN": {
    // Header
    "header.title": "全视产品化平台",
    "header.subtitle": "神经指挥中心 v2.0",
    "header.pipeline": "项目流水线",
    "header.complete": "已完成",
    
    // Panels
    "panel.host": "主机与设备",
    "panel.spec": "规格矩阵",
    "panel.agents": "代理矩阵",
    "panel.orchestrator": "协调器 AI",
    "panel.tasks": "任务积压",
    "panel.source": "源代码控制",
    "panel.vitals": "状态与产物",
    
    // Orchestrator
    "orchestrator.title": "协调器 AI",
    "orchestrator.subtitle": "中央协调员",
    "orchestrator.agents": "代理",
    "orchestrator.suggestions": "AI 建议",
    "orchestrator.placeholder": "输入命令或消息...",
    "orchestrator.accept": "接受",
    "orchestrator.reject": "拒绝",
    "orchestrator.reassign": "重新分配",
    
    // Status
    "status.idle": "空闲",
    "status.running": "运行中",
    "status.completed": "已完成",
    "status.error": "错误",
    "status.pending": "待处理",
    "status.synced": "已同步",
    
    // Actions
    "action.spawn": "生成",
    "action.assign": "分配",
    "action.stop": "停止",
    "action.resume": "恢复",
    
    // Common
    "common.loading": "加载中...",
    "common.error": "错误",
    "common.success": "成功",
    "common.cancel": "取消",
    "common.confirm": "确认",
    "common.save": "保存",
    "common.delete": "删除",
    "common.add": "添加",
    "common.remove": "移除",
  },
  
  "zh-TW": {
    // Header
    "header.title": "全視產品化平台",
    "header.subtitle": "神經指揮中心 v2.0",
    "header.pipeline": "專案流水線",
    "header.complete": "已完成",
    
    // Panels
    "panel.host": "主機與裝置",
    "panel.spec": "規格矩陣",
    "panel.agents": "代理矩陣",
    "panel.orchestrator": "協調器 AI",
    "panel.tasks": "任務積壓",
    "panel.source": "原始碼控制",
    "panel.vitals": "狀態與產物",
    
    // Orchestrator
    "orchestrator.title": "協調器 AI",
    "orchestrator.subtitle": "中央協調員",
    "orchestrator.agents": "代理",
    "orchestrator.suggestions": "AI 建議",
    "orchestrator.placeholder": "輸入命令或訊息...",
    "orchestrator.accept": "接受",
    "orchestrator.reject": "拒絕",
    "orchestrator.reassign": "重新指派",
    
    // Status
    "status.idle": "閒置",
    "status.running": "執行中",
    "status.completed": "已完成",
    "status.error": "錯誤",
    "status.pending": "待處理",
    "status.synced": "已同步",
    
    // Actions
    "action.spawn": "產生",
    "action.assign": "指派",
    "action.stop": "停止",
    "action.resume": "恢復",
    
    // Common
    "common.loading": "載入中...",
    "common.error": "錯誤",
    "common.success": "成功",
    "common.cancel": "取消",
    "common.confirm": "確認",
    "common.save": "儲存",
    "common.delete": "刪除",
    "common.add": "新增",
    "common.remove": "移除",
  },
  
  ja: {
    // Header
    "header.title": "オムニサイト プロダクタイザー",
    "header.subtitle": "ニューラルコマンドセンター v2.0",
    "header.pipeline": "プロジェクトパイプライン",
    "header.complete": "完了",
    
    // Panels
    "panel.host": "ホストとデバイス",
    "panel.spec": "スペックマトリックス",
    "panel.agents": "エージェントマトリックス",
    "panel.orchestrator": "オーケストレーターAI",
    "panel.tasks": "タスクバックログ",
    "panel.source": "ソースコントロール",
    "panel.vitals": "バイタルとアーティファクト",
    
    // Orchestrator
    "orchestrator.title": "オーケストレーターAI",
    "orchestrator.subtitle": "セントラルコーディネーター",
    "orchestrator.agents": "エージェント",
    "orchestrator.suggestions": "AI提案",
    "orchestrator.placeholder": "コマンドまたはメッセージを入力...",
    "orchestrator.accept": "承認",
    "orchestrator.reject": "拒否",
    "orchestrator.reassign": "再割当",
    
    // Status
    "status.idle": "待機中",
    "status.running": "実行中",
    "status.completed": "完了",
    "status.error": "エラー",
    "status.pending": "保留中",
    "status.synced": "同期済み",
    
    // Actions
    "action.spawn": "生成",
    "action.assign": "割当",
    "action.stop": "停止",
    "action.resume": "再開",
    
    // Common
    "common.loading": "読み込み中...",
    "common.error": "エラー",
    "common.success": "成功",
    "common.cancel": "キャンセル",
    "common.confirm": "確認",
    "common.save": "保存",
    "common.delete": "削除",
    "common.add": "追加",
    "common.remove": "削除",
  },
}

// Detect browser language
function detectBrowserLocale(): Locale {
  if (typeof window === "undefined") return "en"
  
  const browserLang = navigator.language || (navigator as unknown as { userLanguage?: string }).userLanguage || "en"
  
  // Check for exact match first
  if (browserLang in translations) {
    return browserLang as Locale
  }
  
  // Check for language prefix match
  const langPrefix = browserLang.split("-")[0]
  if (langPrefix === "zh") {
    // Default to Simplified Chinese for zh without region
    return browserLang.includes("TW") || browserLang.includes("HK") ? "zh-TW" : "zh-CN"
  }
  if (langPrefix === "ja") return "ja"
  
  return "en"
}

interface I18nProviderProps {
  children: ReactNode
}

export function I18nProvider({ children }: I18nProviderProps) {
  const [locale, setLocaleState] = useState<Locale>("en")
  const [mounted, setMounted] = useState(false)
  
  // Initialize locale from localStorage or browser detection after mount
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- mount-time hydration from localStorage/browser detection
    setMounted(true)
    try {
      const savedLocale = localStorage.getItem("omnisight-locale") as Locale | null
      const detectedLocale = savedLocale || detectBrowserLocale()
      setLocaleState(detectedLocale)
      document.documentElement.lang = detectedLocale
    } catch {
      // localStorage not available, use default
    }
  }, [])
  
  // Persist locale changes
  const setLocale = (newLocale: Locale) => {
    setLocaleState(newLocale)
    try {
      localStorage.setItem("omnisight-locale", newLocale)
    } catch {
      // localStorage not available
    }
    if (typeof document !== "undefined") {
      document.documentElement.lang = newLocale
    }
  }
  
  // Translation function with parameter support
  const t = (key: string, params?: Record<string, string | number>): string => {
    const currentLocale = mounted ? locale : "en"
    const dict = translations[currentLocale] || translations.en
    let text = dict[key] || translations.en[key] || key
    
    // Replace parameters like {name} with values
    if (params) {
      Object.entries(params).forEach(([paramKey, value]) => {
        text = text.replace(new RegExp(`\\{${paramKey}\\}`, "g"), String(value))
      })
    }
    
    return text
  }
  
  return (
    <I18nContext.Provider value={{ locale, setLocale, t }}>
      {children}
    </I18nContext.Provider>
  )
}

export function useI18n() {
  const context = useContext(I18nContext)
  if (!context) {
    throw new Error("useI18n must be used within an I18nProvider")
  }
  return context
}
