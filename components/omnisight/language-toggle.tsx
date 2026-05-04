"use client"

import { useState, useRef, useEffect } from "react"
import { useTranslations } from "next-intl"
import { Globe, ChevronDown, Check } from "lucide-react"
import { useI18n, type Locale } from "@/lib/i18n/context"

interface LanguageOption {
  code: Locale
  // FX.9.9: `labelKey` resolves to a localized description of the
  // language in the *user's currently active* locale (e.g. "簡體中文"
  // when the user's UI is zh-TW). `nativeLabel` is always the language
  // name in its own script and never gets translated. `flag` is the
  // single-character glyph shown in the compact pill.
  labelKey: string
  nativeLabel: string
  flag: string
}

const languages: LanguageOption[] = [
  { code: "en", labelKey: "english", nativeLabel: "English", flag: "EN" },
  { code: "zh-CN", labelKey: "chineseSimplified", nativeLabel: "简体中文", flag: "简" },
  { code: "zh-TW", labelKey: "chineseTraditional", nativeLabel: "繁體中文", flag: "繁" },
  { code: "ja", labelKey: "japanese", nativeLabel: "日本語", flag: "日" },
]

interface LanguageToggleProps {
  compact?: boolean
}

export function LanguageToggle({ compact = false }: LanguageToggleProps) {
  const { locale, setLocale } = useI18n()
  const tLang = useTranslations("language")
  const [isOpen, setIsOpen] = useState(false)
  const dropdownRef = useRef<HTMLDivElement>(null)

  const currentLang = languages.find(l => l.code === locale) || languages[0]
  
  // Close dropdown when clicking outside
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target as Node)) {
        setIsOpen(false)
      }
    }
    
    document.addEventListener("mousedown", handleClickOutside)
    return () => document.removeEventListener("mousedown", handleClickOutside)
  }, [])
  
  const handleSelect = (code: Locale) => {
    setLocale(code)
    setIsOpen(false)
  }
  
  if (compact) {
    // Compact version for mobile - opens downward from header
    return (
      <div className="relative" ref={dropdownRef}>
        <button
          onClick={() => setIsOpen(!isOpen)}
          className="flex items-center justify-center w-8 h-8 rounded-lg bg-[var(--secondary)] text-[var(--neural-blue)] hover:bg-[var(--neural-blue)]/20 transition-colors"
          aria-label={tLang("selectLanguage")}
        >
          <span className="font-mono text-[10px] font-bold">{currentLang.flag}</span>
        </button>
        
        {isOpen && (
          <div className="absolute top-full mt-2 right-0 min-w-[160px] holo-glass-simple rounded-lg py-1 z-50 shadow-lg border border-[var(--border)]">
            {languages.map(lang => (
              <button
                key={lang.code}
                onClick={() => handleSelect(lang.code)}
                className={`w-full flex items-center gap-3 px-3 py-2.5 text-left transition-colors ${
                  lang.code === locale 
                    ? "bg-[var(--neural-blue)]/20 text-[var(--neural-blue)]" 
                    : "hover:bg-[var(--secondary)] text-[var(--foreground)]"
                }`}
              >
                <span className="font-mono text-xs font-bold w-6">{lang.flag}</span>
                <span className="font-mono text-sm flex-1">{lang.nativeLabel}</span>
                {lang.code === locale && <Check size={14} className="text-[var(--neural-blue)]" />}
              </button>
            ))}
          </div>
        )}
      </div>
    )
  }
  
  // Full version for desktop
  return (
    <div className="relative" ref={dropdownRef}>
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="flex items-center gap-2 px-3 py-1.5 rounded bg-[var(--secondary)] hover:bg-[var(--neural-blue)]/20 transition-colors group"
        aria-label={tLang("selectLanguage")}
      >
        <Globe size={14} className="text-[var(--muted-foreground)] group-hover:text-[var(--neural-blue)]" />
        <span className="font-mono text-xs text-[var(--foreground)]">{currentLang.flag}</span>
        <ChevronDown size={12} className={`text-[var(--muted-foreground)] transition-transform ${isOpen ? "rotate-180" : ""}`} />
      </button>
      
      {isOpen && (
        <div className="absolute top-full mt-1 right-0 min-w-[180px] holo-glass-simple rounded-lg py-1 z-50">
          {languages.map(lang => (
            <button
              key={lang.code}
              onClick={() => handleSelect(lang.code)}
              className={`w-full flex items-center gap-3 px-3 py-2 text-left transition-colors ${
                lang.code === locale 
                  ? "bg-[var(--neural-blue)]/20 text-[var(--neural-blue)]" 
                  : "hover:bg-[var(--secondary)] text-[var(--foreground)]"
              }`}
            >
              <span className="font-mono text-xs font-bold w-6">{lang.flag}</span>
              <div className="flex-1">
                <div className="font-mono text-sm">{lang.nativeLabel}</div>
                <div className="font-mono text-xs text-[var(--muted-foreground)]">{tLang(lang.labelKey)}</div>
              </div>
              {lang.code === locale && <Check size={14} className="text-[var(--neural-blue)]" />}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
