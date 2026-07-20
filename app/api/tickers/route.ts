import { NextRequest, NextResponse } from 'next/server'
import { createServiceClient } from '@/lib/supabase-server'

export async function GET() {
  const supabase = createServiceClient()
  const { data, error } = await supabase
    .from('tickers')
    .select('*')
    .eq('active', true)
    .order('symbol')

  if (error) return NextResponse.json({ error: error.message }, { status: 500 })
  return NextResponse.json(data)
}

export async function POST(req: NextRequest) {
  const { symbol, name } = await req.json()
  if (!symbol) return NextResponse.json({ error: 'symbol required' }, { status: 400 })

  const supabase = createServiceClient()
  const { data, error } = await supabase
    .from('tickers')
    .insert({ symbol: symbol.toUpperCase(), name })
    .select()
    .single()

  if (error) return NextResponse.json({ error: error.message }, { status: 500 })
  return NextResponse.json(data, { status: 201 })
}

export async function DELETE(req: NextRequest) {
  const { symbol } = await req.json()
  const supabase = createServiceClient()
  const { error } = await supabase
    .from('tickers')
    .update({ active: false })
    .eq('symbol', symbol)

  if (error) return NextResponse.json({ error: error.message }, { status: 500 })
  return NextResponse.json({ ok: true })
}
