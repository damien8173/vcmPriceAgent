'use client'

import { useEffect, useState } from 'react'
import type { Ticker, Dividend } from '@/types'

export default function DashboardPage() {
  const [tickers,   setTickers]   = useState<Ticker[]>([])
  const [dividends, setDividends] = useState<Dividend[]>([])
  const [newSymbol, setNewSymbol] = useState('')
  const [loading,   setLoading]   = useState(true)

  useEffect(() => {
    Promise.all([
      fetch('/api/tickers').then(r => r.json()),
      fetch('/api/filings?days=60').then(r => r.json()),
    ]).then(([t, d]) => {
      setTickers(t)
      setDividends(d?.dividends ?? [])
      setLoading(false)
    })
  }, [])

  async function addTicker() {
    if (!newSymbol.trim()) return
    await fetch('/api/tickers', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol: newSymbol.trim() }),
    })
    setNewSymbol('')
    const updated = await fetch('/api/tickers').then(r => r.json())
    setTickers(updated)
  }

  async function removeTicker(symbol: string) {
    await fetch('/api/tickers', {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol }),
    })
    setTickers(t => t.filter(x => x.symbol !== symbol))
  }

  if (loading) return <p className="text-gray-500">Loading...</p>

  return (
    <div className="space-y-10">
      <section>
        <h1 className="text-2xl font-bold mb-4">Watched Tickers</h1>
        <div className="flex gap-2 mb-4">
          <input
            className="bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm w-48 focus:outline-none focus:border-brand-500"
            placeholder="0005.HK"
            value={newSymbol}
            onChange={e => setNewSymbol(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && addTicker()}
          />
          <button
            onClick={addTicker}
            className="bg-brand-500 hover:bg-brand-500/80 text-white px-4 py-2 rounded text-sm font-medium"
          >
            Add
          </button>
        </div>

        <div className="flex flex-wrap gap-2">
          {tickers.map(t => (
            <span key={t.id} className="flex items-center gap-1 bg-gray-800 border border-gray-700 rounded-full px-3 py-1 text-sm">
              {t.symbol}
              <button onClick={() => removeTicker(t.symbol)} className="text-gray-500 hover:text-red-400 ml-1">×</button>
            </span>
          ))}
          {tickers.length === 0 && <p className="text-gray-500 text-sm">No tickers watched yet.</p>}
        </div>
      </section>

      <section>
        <h2 className="text-xl font-bold mb-4">Upcoming Dividends (60 days)</h2>
        {dividends.length === 0 ? (
          <p className="text-gray-500 text-sm">No upcoming dividends found.</p>
        ) : (
          <table className="w-full text-sm border-collapse">
            <thead>
              <tr className="text-left text-gray-400 border-b border-gray-800">
                <th className="py-2 pr-4">Ticker</th>
                <th className="py-2 pr-4">Type</th>
                <th className="py-2 pr-4">Amount</th>
                <th className="py-2 pr-4">Ex-Date</th>
                <th className="py-2">Payment Date</th>
              </tr>
            </thead>
            <tbody>
              {dividends.map(d => (
                <tr key={d.id} className="border-b border-gray-900 hover:bg-gray-900/40">
                  <td className="py-2 pr-4 font-mono">{d.ticker_symbol}</td>
                  <td className="py-2 pr-4 capitalize">{d.dividend_type}</td>
                  <td className="py-2 pr-4">{d.currency} {d.amount}</td>
                  <td className="py-2 pr-4">{d.ex_date ?? '—'}</td>
                  <td className="py-2">{d.payment_date ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  )
}
