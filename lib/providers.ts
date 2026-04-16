/**
 * AI Provider registry for the Vercel AI SDK frontend.
 *
 * Each provider can be used directly from the frontend via Next.js API routes,
 * or requests can be routed through the Python backend (default).
 */

import { anthropic } from "@ai-sdk/anthropic"
import { google } from "@ai-sdk/google"
import { openai } from "@ai-sdk/openai"
import { xai } from "@ai-sdk/xai"
import { createGroq } from "@ai-sdk/groq"
import { deepseek } from "@ai-sdk/deepseek"
import { togetherai } from "@ai-sdk/togetherai"
import { ollama } from "ollama-ai-provider"
import type { LanguageModel } from "ai"

export type ProviderId =
  | "anthropic"
  | "google"
  | "openai"
  | "xai"
  | "groq"
  | "deepseek"
  | "together"
  | "ollama"

export interface ProviderInfo {
  id: ProviderId
  name: string
  defaultModel: string
  models: string[]
  envVar: string | null
}

export const PROVIDERS: ProviderInfo[] = [
  {
    id: "anthropic",
    name: "Anthropic",
    defaultModel: "claude-sonnet-4-20250514",
    models: [
      "claude-opus-4-7",
      "claude-opus-4-20250514",
      "claude-sonnet-4-20250514",
      "claude-haiku-4-20250506",
    ],
    envVar: "ANTHROPIC_API_KEY",
  },
  {
    id: "google",
    name: "Google Gemini",
    defaultModel: "gemini-1.5-pro",
    models: ["gemini-1.5-pro", "gemini-1.5-flash", "gemini-2.5-pro-preview-05-06"],
    envVar: "GOOGLE_GENERATIVE_AI_API_KEY",
  },
  {
    id: "openai",
    name: "OpenAI",
    defaultModel: "gpt-4o",
    models: ["gpt-4o", "gpt-4o-mini", "o3-mini"],
    envVar: "OPENAI_API_KEY",
  },
  {
    id: "xai",
    name: "xAI (Grok)",
    defaultModel: "grok-3-mini",
    models: ["grok-3", "grok-3-mini"],
    envVar: "XAI_API_KEY",
  },
  {
    id: "groq",
    name: "Groq",
    defaultModel: "llama-3.3-70b-versatile",
    models: ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768"],
    envVar: "GROQ_API_KEY",
  },
  {
    id: "deepseek",
    name: "DeepSeek",
    defaultModel: "deepseek-chat",
    models: ["deepseek-chat", "deepseek-reasoner"],
    envVar: "DEEPSEEK_API_KEY",
  },
  {
    id: "together",
    name: "Together.ai",
    defaultModel: "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
    models: [
      "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
      "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo",
      "Qwen/Qwen2.5-72B-Instruct-Turbo",
    ],
    envVar: "TOGETHER_AI_API_KEY",
  },
  {
    id: "ollama",
    name: "Ollama (Local)",
    defaultModel: "llama3.1",
    models: ["llama3.1", "llama3.2", "qwen2.5", "mistral", "codellama", "deepseek-r1"],
    envVar: null,
  },
]

const groq = createGroq()

/**
 * Get a Vercel AI SDK model instance for the given provider and model name.
 */
export function getModel(
  providerId: ProviderId,
  modelName?: string
): LanguageModel {
  const info = PROVIDERS.find((p) => p.id === providerId)
  const model = modelName || info?.defaultModel || ""

  switch (providerId) {
    case "anthropic":
      return anthropic(model)
    case "google":
      return google(model)
    case "openai":
      return openai(model)
    case "xai":
      return xai(model)
    case "groq":
      return groq(model)
    case "deepseek":
      return deepseek(model)
    case "together":
      return togetherai(model)
    case "ollama":
      // ollama-ai-provider still emits the legacy `LanguageModelV1`
      // shape — structurally compatible with `LanguageModel` for
      // streamText's purposes. Cast through unknown until the
      // upstream package is updated.
      return ollama(model) as unknown as LanguageModel
    default:
      return anthropic(model)
  }
}
