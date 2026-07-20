-- Watchlist targets — ticker + date pairs the daemon monitors actively
CREATE TABLE targets (
  id           UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  symbol       TEXT NOT NULL,
  target_date  DATE,
  status       TEXT DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
  name         TEXT,
  created_at   TIMESTAMPTZ DEFAULT NOW()
);

-- HKEX filings cache — populated by the polling daemon
-- filing_id is the 16-char hex ID from HKEX (used for deduplication)
CREATE TABLE exchange_filing (
  id                    UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  filing_id             TEXT UNIQUE NOT NULL,
  stock_code            TEXT,
  stock_name            TEXT,
  title                 TEXT,
  filing_date           TIMESTAMPTZ,
  document_url          TEXT,
  document_text         TEXT,
  document_status       TEXT DEFAULT 'pending'
                          CHECK (document_status IN ('pending', 'extracted', 'failed', 'skipped')),
  document_type         TEXT,
  document_text_len     INTEGER,
  document_status_reason TEXT,
  updated_at            TIMESTAMPTZ DEFAULT NOW()
);

-- LLM-extracted facts from filings
-- One row per filing; event_kind determines which fields are populated
CREATE TABLE company_event (
  id                  UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  filing_id           TEXT UNIQUE NOT NULL REFERENCES exchange_filing(filing_id) ON DELETE CASCADE,
  stock_code          TEXT,
  stock_name          TEXT,
  company_ticker      TEXT,
  title               TEXT,
  document_url        TEXT,
  announcement_date   TIMESTAMPTZ,
  event_kind          TEXT CHECK (event_kind IN ('board_meeting', 'results', 'dividend', 'other')),
  -- board meeting fields
  board_meeting_date    DATE,
  board_meeting_purpose TEXT,
  -- results fields
  results_period        TEXT CHECK (results_period IN ('annual', 'interim', 'quarterly')),
  -- dividend fields
  dividend_type         TEXT,
  dividend_amount       TEXT,
  ex_date               DATE,
  record_date           DATE,
  payment_date          DATE,
  declared_date         DATE,
  -- extraction metadata
  extraction_status     TEXT DEFAULT 'ok' CHECK (extraction_status IN ('ok', 'failed')),
  updated_at            TIMESTAMPTZ DEFAULT NOW()
);

-- Daily watchlist rankings — regenerated each day by the scoring engine
CREATE TABLE dividend_watchlist (
  id             UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  watchlist_date DATE NOT NULL,
  rank           INTEGER,
  stock_code     TEXT,
  stock_name     TEXT,
  score          INTEGER,
  band           TEXT CHECK (band IN ('High', 'Medium', 'Low')),
  reasons        JSONB,
  generated_at   TIMESTAMPTZ DEFAULT NOW()
);

-- Notification dispatch log
CREATE TABLE alert_history (
  id            UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  filing_id     TEXT,
  stock_code    TEXT,
  channel       TEXT CHECK (channel IN ('slack', 'discord', 'telegram')),
  alert_type    TEXT CHECK (alert_type IN ('filing_ping', 'dividend_alert', 'outage')),
  payload       JSONB,
  sent_at       TIMESTAMPTZ DEFAULT NOW(),
  success       BOOLEAN,
  error_message TEXT
);

-- Indexes for common access patterns
CREATE INDEX idx_exchange_filing_stock_code  ON exchange_filing(stock_code);
CREATE INDEX idx_exchange_filing_filing_date ON exchange_filing(filing_date DESC);
CREATE INDEX idx_exchange_filing_status      ON exchange_filing(document_status);

CREATE INDEX idx_company_event_stock_code    ON company_event(stock_code);
CREATE INDEX idx_company_event_ex_date       ON company_event(ex_date);
CREATE INDEX idx_company_event_event_kind    ON company_event(event_kind);

CREATE INDEX idx_watchlist_date              ON dividend_watchlist(watchlist_date DESC);
CREATE INDEX idx_watchlist_score             ON dividend_watchlist(score DESC);

CREATE INDEX idx_targets_symbol              ON targets(symbol);
CREATE INDEX idx_targets_status              ON targets(status);
CREATE INDEX idx_targets_date               ON targets(target_date);
