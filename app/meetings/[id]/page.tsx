"use client"

/**
 * Post-meeting intelligence view (BI1-4).
 *
 * Given a meeting id, shows the meeting envelope and lets the operator
 * generate the AI insights produced over the stored transcript:
 * summary (BI1), action items + discussion points (BI3), facilitation
 * suggestions (BI4) and a translation (BI2). Each backend surface is
 * disabled-by-default; a 404 "feature not enabled" is surfaced as a
 * gentle hint rather than an error toast.
 *
 * Styled with the OmniSight "Orbital" palette (Azure / Nebula / Aurora on
 * deep-space neutrals) for brand consistency with the appliance UI.
 */

import { useCallback, useEffect, useState } from "react"
import { useParams } from "next/navigation"
import {
  AlignLeft,
  CheckSquare,
  Languages,
  Lightbulb,
  Loader2,
  Mic,
  RefreshCw,
} from "lucide-react"
import {
  generateMeetingActionItems,
  generateMeetingSuggestions,
  generateMeetingSummary,
  getMeeting,
  translateMeeting,
  type ActionItemsResult,
  type MeetingEnvelope,
  type MeetingSummary,
  type SuggestionsResult,
  type TranslationResult,
} from "@/lib/api"

type Loading = null | "summary" | "actions" | "suggestions" | "translate"

function errText(e: unknown): string {
  const msg = e instanceof Error ? e.message : String(e)
  if (/not enabled/i.test(msg)) return "此功能在後端尚未啟用(disabled-by-default)。"
  return msg || "發生未知錯誤"
}

export default function MeetingInsightsPage() {
  const params = useParams<{ id: string }>()
  const meetingId = decodeURIComponent(params?.id ?? "")

  const [envelope, setEnvelope] = useState<MeetingEnvelope | null>(null)
  const [envErr, setEnvErr] = useState<string | null>(null)
  const [loading, setLoading] = useState<Loading>(null)
  const [error, setError] = useState<string | null>(null)

  const [summary, setSummary] = useState<MeetingSummary | null>(null)
  const [actions, setActions] = useState<ActionItemsResult | null>(null)
  const [suggestions, setSuggestions] = useState<SuggestionsResult | null>(null)
  const [translation, setTranslation] = useState<TranslationResult | null>(null)
  const [targetLang, setTargetLang] = useState("zh-Hant")

  const loadEnvelope = useCallback(async () => {
    if (!meetingId) return
    try {
      setEnvErr(null)
      setEnvelope(await getMeeting(meetingId))
    } catch (e) {
      setEnvErr(errText(e))
    }
  }, [meetingId])

  useEffect(() => { void loadEnvelope() }, [loadEnvelope])

  async function run(kind: Loading, fn: () => Promise<void>) {
    setLoading(kind)
    setError(null)
    try {
      await fn()
    } catch (e) {
      setError(errText(e))
    } finally {
      setLoading(null)
    }
  }

  const disabled = loading !== null

  return (
    <main className="min-h-screen bg-[#070C18] text-[#EAF2FF] px-6 py-8">
      {/* brand stripe */}
      <div
        className="h-[3px] w-full rounded mb-6"
        style={{ background: "linear-gradient(90deg,#1D9BF0 0%,#7C5CFF 55%,#22D3EE 100%)" }}
      />

      <header className="mb-6 flex items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2 text-sm text-[#8B9AC0]">
            <Mic size={16} className="text-[#22D3EE]" />
            會議洞察 · Meeting intelligence
          </div>
          <h1 className="text-3xl font-bold mt-1">
            {envelope?.title || meetingId || "—"}
          </h1>
          {envelope && (
            <p className="text-sm text-[#8B9AC0] mt-2">
              狀態 {envelope.status} · 共 {envelope.segment_count} 段
              （{envelope.final_segment_count} 已定稿）
              {envelope.languages.length > 0 && ` · 語言 ${envelope.languages.join(", ")}`}
            </p>
          )}
          {envErr && <p className="text-sm text-[#F2495C] mt-2">無法載入會議：{envErr}</p>}
        </div>
        <button
          onClick={() => void loadEnvelope()}
          className="flex items-center gap-2 rounded-lg border border-[#2A3C63] bg-[#16213B] px-3 py-2 text-sm hover:bg-[#1D2B4B]"
        >
          <RefreshCw size={15} /> 重新整理
        </button>
      </header>

      {error && (
        <div className="mb-4 rounded-lg border border-[#4A2436] bg-[#1F1426] px-4 py-3 text-sm text-[#F2495C]">
          {error}
        </div>
      )}

      {/* action bar */}
      <div className="flex flex-wrap gap-3 mb-8">
        <GenButton
          icon={<AlignLeft size={16} />} label="摘要" busy={loading === "summary"}
          disabled={disabled}
          onClick={() => run("summary", async () =>
            setSummary(await generateMeetingSummary(meetingId)))}
        />
        <GenButton
          icon={<CheckSquare size={16} />} label="Action items" busy={loading === "actions"}
          disabled={disabled}
          onClick={() => run("actions", async () =>
            setActions(await generateMeetingActionItems(meetingId)))}
        />
        <GenButton
          icon={<Lightbulb size={16} />} label="建議" busy={loading === "suggestions"}
          disabled={disabled}
          onClick={() => run("suggestions", async () =>
            setSuggestions(await generateMeetingSuggestions(meetingId)))}
        />
        <div className="flex items-center gap-2">
          <GenButton
            icon={<Languages size={16} />} label="翻譯" busy={loading === "translate"}
            disabled={disabled}
            onClick={() => run("translate", async () =>
              setTranslation(await translateMeeting(meetingId, targetLang)))}
          />
          <input
            value={targetLang}
            onChange={(e) => setTargetLang(e.target.value)}
            className="w-24 rounded-lg border border-[#243352] bg-[#0A1322] px-3 py-2 text-sm focus:border-[#1D9BF0] outline-none"
            placeholder="zh-Hant"
            aria-label="翻譯目標語言"
          />
        </div>
      </div>

      <div className="grid gap-6 md:grid-cols-2">
        {summary && (
          <Card title="摘要 · Summary" accent="#1D9BF0" footer={summary.model}>
            <p className="text-[#EAF2FF]">{summary.tldr || "（無內容）"}</p>
            {summary.bullet_points.length > 0 && (
              <ul className="mt-3 list-disc pl-5 space-y-1 text-[#C7D3E6]">
                {summary.bullet_points.map((b, i) => <li key={i}>{b}</li>)}
              </ul>
            )}
          </Card>
        )}

        {actions && (
          <Card title="Action items" accent="#7C5CFF" footer={actions.model}>
            {actions.action_items.length === 0
              ? <p className="text-[#8B9AC0]">沒有抓到 action item。</p>
              : <ul className="space-y-2">
                  {actions.action_items.map((a, i) => (
                    <li key={i} className="rounded-lg bg-[#0A1322] px-3 py-2">
                      <span className="text-[#EAF2FF]">{a.text}</span>
                      {(a.owner || a.due) && (
                        <span className="text-xs text-[#8B9AC0] ml-2">
                          {a.owner ? `@${a.owner}` : ""}{a.due ? ` · ${a.due}` : ""}
                        </span>
                      )}
                    </li>
                  ))}
                </ul>}
            {actions.discussion_points.length > 0 && (
              <div className="mt-4">
                <div className="text-xs uppercase tracking-wide text-[#8B9AC0] mb-2">討論點</div>
                <ul className="space-y-1 text-[#C7D3E6]">
                  {actions.discussion_points.map((d, i) => (
                    <li key={i}><b className="text-[#EAF2FF]">{d.topic}</b> — {d.summary}</li>
                  ))}
                </ul>
              </div>
            )}
          </Card>
        )}

        {suggestions && (
          <Card title="建議 · Suggestions" accent="#22D3EE" footer={suggestions.model}>
            {suggestions.suggestions.length === 0
              ? <p className="text-[#8B9AC0]">沒有建議。</p>
              : <ul className="space-y-2">
                  {suggestions.suggestions.map((s, i) => (
                    <li key={i} className="rounded-lg bg-[#0A1322] px-3 py-2">
                      <span className="text-[10px] uppercase tracking-wide text-[#22D3EE] mr-2">{s.kind}</span>
                      <span className="text-[#EAF2FF]">{s.text}</span>
                    </li>
                  ))}
                </ul>}
          </Card>
        )}

        {translation && (
          <Card title={`翻譯 · ${translation.target_lang}`} accent="#1D9BF0" footer={translation.model ?? undefined}>
            <p className="whitespace-pre-wrap text-[#EAF2FF]">{translation.text || "（無內容）"}</p>
          </Card>
        )}
      </div>

      {!summary && !actions && !suggestions && !translation && !error && (
        <p className="text-[#8B9AC0] text-sm">
          選擇上方任一動作,從這場會議的逐字稿產生 AI 洞察。
        </p>
      )}
    </main>
  )
}

function GenButton(props: {
  icon: React.ReactNode; label: string; busy: boolean; disabled: boolean
  onClick: () => void
}) {
  return (
    <button
      onClick={props.onClick}
      disabled={props.disabled}
      className="flex items-center gap-2 rounded-xl px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-50"
      style={{ background: "linear-gradient(135deg,#1D9BF0 0%,#6A5CFF 100%)" }}
    >
      {props.busy ? <Loader2 size={16} className="animate-spin" /> : props.icon}
      {props.label}
    </button>
  )
}

function Card(props: {
  title: string; accent: string; footer?: string; children: React.ReactNode
}) {
  return (
    <section
      className="rounded-2xl border border-[#213152] bg-[#111A30] p-5"
      style={{ borderLeft: `4px solid ${props.accent}` }}
    >
      <h2 className="text-lg font-bold mb-3">{props.title}</h2>
      {props.children}
      {props.footer && (
        <div className="mt-4 text-[11px] text-[#54648A]">model: {props.footer}</div>
      )}
    </section>
  )
}
