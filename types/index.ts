export interface Ticker {
  id: string
  symbol: string       // e.g. "0005.HK"
  name: string | null
  active: boolean
  created_at: string
}

export interface Filing {
  id: string
  hkex_id: string
  ticker_symbol: string
  title: string | null
  filing_url: string | null
  published_at: string | null
  processed: boolean
  created_at: string
}

export interface Dividend {
  id: string
  filing_id: string
  ticker_symbol: string
  dividend_type: 'interim' | 'final' | 'special' | string
  amount: number | null
  currency: string
  ex_date: string | null
  record_date: string | null
  payment_date: string | null
  raw_text: string | null
  created_at: string
}

export interface Alert {
  id: string
  dividend_id: string
  channel: 'slack' | 'discord' | 'telegram'
  sent_at: string
  success: boolean
  error_message: string | null
}

export interface NotificationConfig {
  id: string
  channel: 'slack' | 'discord' | 'telegram'
  webhook_url: string | null
  bot_token: string | null
  chat_id: string | null
  active: boolean
}

export interface ChatMessage {
  role: 'user' | 'assistant' | 'tool'
  content: string
  tool_call_id?: string
  tool_calls?: ToolCall[]
}

export interface ToolCall {
  id: string
  type: 'function'
  function: {
    name: string
    arguments: string
  }
}
