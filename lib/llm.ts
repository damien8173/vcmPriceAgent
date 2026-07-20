import OpenAI from 'openai'

// DeepSeek uses the OpenAI-compatible API — swap base_url for any other provider
export const llm = new OpenAI({
  apiKey:  process.env.LLM_API_KEY!,
  baseURL: process.env.LLM_BASE_URL ?? 'https://api.deepseek.com',
})

export const LLM_MODEL = process.env.LLM_MODEL ?? 'deepseek-chat'

export const SYSTEM_PROMPT = `You are an AI assistant for a Hong Kong stock exchange (HKEX) dividend monitoring system.
You help users understand dividend announcements, upcoming ex-dates, and filing details for HK-listed companies.
When you need live data (filings, upcoming dividends, watched tickers), use the available tools — never guess ticker symbols or dates.
Be concise and precise. Format dates as YYYY-MM-DD. Currency amounts should include the currency code (e.g. HKD 0.30).`
