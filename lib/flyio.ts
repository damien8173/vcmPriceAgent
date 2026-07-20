const BASE_URL = process.env.FLYIO_API_URL!
const SECRET   = process.env.FLYIO_API_SECRET!

async function flyFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      'X-Internal-Secret': SECRET,
      ...init?.headers,
    },
  })
  if (!res.ok) throw new Error(`Fly.io ${path} → ${res.status}`)
  return res.json()
}

export const flyio = {
  getFilings: (ticker: string, limit = 10) =>
    flyFetch<{ filings: unknown[] }>(`/filings?ticker=${ticker}&limit=${limit}`),

  getUpcomingDividends: (days = 30) =>
    flyFetch<{ dividends: unknown[] }>(`/dividends/upcoming?days=${days}`),

  getTickers: () =>
    flyFetch<{ tickers: string[] }>('/tickers'),

  triggerPoll: () =>
    flyFetch<{ status: string }>('/poll', { method: 'POST' }),
}
