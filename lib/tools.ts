import { flyio } from './flyio'

// Tool definitions sent to the LLM
export const toolDefinitions = [
  {
    type: 'function' as const,
    function: {
      name: 'get_filings',
      description: 'Fetch recent HKEX filings for a specific ticker symbol',
      parameters: {
        type: 'object',
        properties: {
          ticker: { type: 'string', description: 'HKEX ticker, e.g. 0005.HK' },
          limit:  { type: 'number', description: 'Max results (default 10)' },
        },
        required: ['ticker'],
      },
    },
  },
  {
    type: 'function' as const,
    function: {
      name: 'get_upcoming_dividends',
      description: 'List upcoming dividend ex-dates across all watched tickers',
      parameters: {
        type: 'object',
        properties: {
          days: { type: 'number', description: 'Look-ahead window in days (default 30)' },
        },
        required: [],
      },
    },
  },
  {
    type: 'function' as const,
    function: {
      name: 'get_watched_tickers',
      description: 'Return the list of tickers currently being monitored',
      parameters: { type: 'object', properties: {}, required: [] },
    },
  },
]

// Execute a tool call and return the result as a string
export async function executeTool(name: string, args: Record<string, unknown>): Promise<string> {
  switch (name) {
    case 'get_filings': {
      const data = await flyio.getFilings(args.ticker as string, (args.limit as number) ?? 10)
      return JSON.stringify(data)
    }
    case 'get_upcoming_dividends': {
      const data = await flyio.getUpcomingDividends((args.days as number) ?? 30)
      return JSON.stringify(data)
    }
    case 'get_watched_tickers': {
      const data = await flyio.getTickers()
      return JSON.stringify(data)
    }
    default:
      return JSON.stringify({ error: `Unknown tool: ${name}` })
  }
}
