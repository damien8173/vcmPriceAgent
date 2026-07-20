'use client'

import { useState, useRef, useEffect } from 'react'
import type { ChatMessage } from '@/types'

interface DisplayMessage {
  role: 'user' | 'assistant'
  content: string
  toolCalls?: string[]
}

export default function ChatPage() {
  const [messages,  setMessages]  = useState<DisplayMessage[]>([])
  const [history,   setHistory]   = useState<ChatMessage[]>([])
  const [input,     setInput]     = useState('')
  const [streaming, setStreaming] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  async function send() {
    const text = input.trim()
    if (!text || streaming) return

    const userMsg: ChatMessage = { role: 'user', content: text }
    const newHistory = [...history, userMsg]
    setHistory(newHistory)
    setMessages(m => [...m, { role: 'user', content: text }])
    setInput('')
    setStreaming(true)

    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ messages: newHistory }),
    })

    const reader = res.body!.getReader()
    const decoder = new TextDecoder()
    let assistantText = ''
    const toolCallNames: string[] = []

    setMessages(m => [...m, { role: 'assistant', content: '' }])

    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      const chunk = decoder.decode(value)
      for (const line of chunk.split('\n')) {
        if (!line.startsWith('data: ')) continue
        const event = JSON.parse(line.slice(6))

        if (event.type === 'text') {
          assistantText += event.content
          setMessages(m => {
            const updated = [...m]
            updated[updated.length - 1] = {
              role: 'assistant',
              content: assistantText,
              toolCalls: toolCallNames,
            }
            return updated
          })
        }

        if (event.type === 'tool_call') {
          toolCallNames.push(event.name)
          setMessages(m => {
            const updated = [...m]
            updated[updated.length - 1] = {
              ...updated[updated.length - 1],
              toolCalls: [...toolCallNames],
            }
            return updated
          })
        }
      }
    }

    setHistory(h => [...h, { role: 'assistant', content: assistantText }])
    setStreaming(false)
  }

  return (
    <div className="flex flex-col h-[calc(100vh-8rem)]">
      <h1 className="text-2xl font-bold mb-4">Chat</h1>

      <div className="flex-1 overflow-y-auto space-y-4 mb-4 pr-1">
        {messages.length === 0 && (
          <p className="text-gray-500 text-sm">
            Ask about dividends, upcoming ex-dates, or any watched ticker.
          </p>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`flex ${m.role === 'user' ? 'justify-end' : 'justify-start'}`}>
            <div
              className={`max-w-2xl rounded-lg px-4 py-3 text-sm whitespace-pre-wrap ${
                m.role === 'user'
                  ? 'bg-brand-500 text-white'
                  : 'bg-gray-800 text-gray-100'
              }`}
            >
              {m.toolCalls && m.toolCalls.length > 0 && (
                <div className="mb-2 flex flex-wrap gap-1">
                  {m.toolCalls.map((tc, j) => (
                    <span key={j} className="text-xs bg-gray-700 text-gray-300 rounded px-2 py-0.5">
                      {tc}
                    </span>
                  ))}
                </div>
              )}
              {m.content || (streaming && m.role === 'assistant' ? '...' : '')}
            </div>
          </div>
        ))}
        <div ref={bottomRef} />
      </div>

      <div className="flex gap-2">
        <input
          className="flex-1 bg-gray-800 border border-gray-700 rounded px-4 py-2 text-sm focus:outline-none focus:border-brand-500"
          placeholder="Ask about dividends..."
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && !e.shiftKey && send()}
          disabled={streaming}
        />
        <button
          onClick={send}
          disabled={streaming}
          className="bg-brand-500 hover:bg-brand-500/80 disabled:opacity-50 text-white px-5 py-2 rounded text-sm font-medium"
        >
          Send
        </button>
      </div>
    </div>
  )
}
