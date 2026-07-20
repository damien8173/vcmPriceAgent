import { NextRequest, NextResponse } from 'next/server'
import { flyio } from '@/lib/flyio'

export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url)
  const ticker = searchParams.get('ticker')
  const days   = parseInt(searchParams.get('days') ?? '30')

  if (ticker) {
    const data = await flyio.getFilings(ticker)
    return NextResponse.json(data)
  }

  const data = await flyio.getUpcomingDividends(days)
  return NextResponse.json(data)
}
