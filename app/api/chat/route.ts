import { NextRequest } from 'next/server'
import { llm, LLM_MODEL, SYSTEM_PROMPT } from '@/lib/llm'
import { toolDefinitions, executeTool } from '@/lib/tools'
import type { ChatMessage } from '@/types'

export const runtime = 'nodejs'
export const maxDuration = 60

export async function POST(req: NextRequest) {
  const { messages }: { messages: ChatMessage[] } = await req.json()

  const response = await llm.chat.completions.create({
    model: LLM_MODEL,
    stream: true,
    tools: toolDefinitions,
    tool_choice: 'auto',
    messages: [
      { role: 'system', content: SYSTEM_PROMPT },
      ...messages,
    ],
  })

  // Stream back to client, handling tool calls mid-stream
  const encoder = new TextEncoder()
  const stream = new ReadableStream({
    async start(controller) {
      const send = (data: object) =>
        controller.enqueue(encoder.encode(`data: ${JSON.stringify(data)}\n\n`))

      let pendingToolCalls: Record<string, { name: string; arguments: string }> = {}

      for await (const chunk of response) {
        const delta = chunk.choices[0]?.delta

        if (delta?.content) {
          send({ type: 'text', content: delta.content })
        }

        if (delta?.tool_calls) {
          for (const tc of delta.tool_calls) {
            const idx = tc.index ?? 0
            if (!pendingToolCalls[idx]) {
              pendingToolCalls[idx] = { name: '', arguments: '' }
            }
            if (tc.function?.name)      pendingToolCalls[idx].name      += tc.function.name
            if (tc.function?.arguments) pendingToolCalls[idx].arguments += tc.function.arguments
          }
        }

        if (chunk.choices[0]?.finish_reason === 'tool_calls') {
          for (const [, tc] of Object.entries(pendingToolCalls)) {
            send({ type: 'tool_call', name: tc.name })
            const args = JSON.parse(tc.arguments || '{}')
            const result = await executeTool(tc.name, args)
            send({ type: 'tool_result', name: tc.name, result })
          }
          pendingToolCalls = {}
        }
      }

      send({ type: 'done' })
      controller.close()
    },
  })

  return new Response(stream, {
    headers: {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache',
      Connection: 'keep-alive',
    },
  })
}
