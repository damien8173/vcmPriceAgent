-- Watched tickers
CREATE TABLE tickers (
  id         UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  symbol     TEXT NOT NULL UNIQUE,
  name       TEXT,
  active     BOOLEAN DEFAULT true,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- HKEX filings cache — hkex_id prevents duplicate processing
CREATE TABLE filings (
  id            UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  hkex_id       TEXT UNIQUE NOT NULL,
  ticker_symbol TEXT REFERENCES tickers(symbol) ON DELETE CASCADE,
  title         TEXT,
  filing_url    TEXT,
  published_at  TIMESTAMPTZ,
  processed     BOOLEAN DEFAULT false,
  created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Extracted dividend records
CREATE TABLE dividends (
  id             UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  filing_id      UUID REFERENCES filings(id) ON DELETE CASCADE,
  ticker_symbol  TEXT,
  dividend_type  TEXT CHECK (dividend_type IN ('interim', 'final', 'special')),
  amount         NUMERIC,
  currency       TEXT DEFAULT 'HKD',
  ex_date        DATE,
  record_date    DATE,
  payment_date   DATE,
  raw_text       TEXT,
  created_at     TIMESTAMPTZ DEFAULT NOW()
);

-- Alert/notification history
CREATE TABLE alerts (
  id            UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  dividend_id   UUID REFERENCES dividends(id) ON DELETE CASCADE,
  channel       TEXT CHECK (channel IN ('slack', 'discord', 'telegram')),
  sent_at       TIMESTAMPTZ DEFAULT NOW(),
  success       BOOLEAN,
  error_message TEXT
);

-- Indexes for common query patterns
CREATE INDEX idx_dividends_ex_date      ON dividends(ex_date);
CREATE INDEX idx_dividends_ticker       ON dividends(ticker_symbol);
CREATE INDEX idx_filings_ticker         ON filings(ticker_symbol);
CREATE INDEX idx_filings_hkex_id        ON filings(hkex_id);
