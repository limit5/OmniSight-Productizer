/**
 * Next.js API Route for direct frontend-to-LLM chat.
 *
 * This is an alternative path that bypasses the Python backend
 * and calls the LLM directly from the Next.js server.
 *
 * The Python backend (LangGraph pipeline) is still the primary path
 * for agent orchestration. This route is for simple direct chat or
 * when you want to use the Vercel AI SDK streaming natively.
 *
 * Usage from frontend:
 *   const { messages, input, handleSubmit } = useChat({ api: "/api/chat" })
 */

import { streamText, type ModelMessage } from "ai"
import { getModel, type ProviderId } from "@/lib/providers"

export async function POST(req: Request) {
  const body = await req.json()
  const {
    messages,
    provider = "anthropic",
    model,
  } = body as {
    // ai SDK v3 renamed the chat message type to ModelMessage; the
    // union of role strings (system / user / assistant / tool) is
    // enforced by the type, so accepting `ModelMessage[]` directly
    // keeps the route honest instead of leaning on a narrower
    // structural type.
    messages: ModelMessage[]
    provider?: ProviderId
    model?: string
  }

  const result = streamText({
    model: getModel(provider, model),
    system:
      "You are the OmniSight AI assistant, specialized in embedded AI camera " +
      "development (UVC/RTSP, Linux drivers, ISP pipelines, sensor integration). " +
      "Be concise and technical.",
    messages,
  })

  // ai SDK v3 dropped `toDataStreamResponse`; the replacement is
  // `toTextStreamResponse` (plain SSE) or the UI-message variant.
  // Text stream is sufficient for useChat's default wire format.
  return result.toTextStreamResponse()
}
