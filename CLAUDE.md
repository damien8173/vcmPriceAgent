# vcmPriceAgent — CLAUDE.md

## What This Is

HKEX Dividend Monitor: watches Hong Kong Stock Exchange filings for tickers and dates you care about, uses AI to extract dividend details, and sends alerts via Slack/Discord/Telegram. Includes a web dashboard and an AI chat interface.

## Architecture (Option B)

```
Browser
  │
  ├── UI / Dashboard ──► Vercel (Next.js)
  │                        │
  │                        ├── /api/chat ──► DeepSeek API (LLM streaming + tool-calling)
  │                        │     └── tool calls ──► Fly.io /api/*
  │                        │
  │                        └── /api/* ──► Fly.io (thin proxy, hides Fly.io URL)
  │
Fly.io (Python FastAPI)
  ├── GET  /filings, /dividends, /targets, /watchlist, /alerts, /status
  ├── POST /poll, /targets
  ├── Daemon: polls HKEX every 3 min; race mode every 30s on ex-date days
  ├── Extraction: DeepSeek two-tier pipeline (fast → reasoning on ambiguous)
  ├── Scoring: pure Python rule engine (0–100, no LLM)
  └── Notifications: Slack / Discord / Telegram webhooks

SurrealDB (Surreal Cloud — managed, free tier)
  ├── exchange_filing     — raw HKEX filings cache
  ├── company_event       — LLM-extracted facts (board meetings, dividends, results)
  └── dividend_watchlist  — daily scored rankings
```

**Key rule:** All business logic (HKEX scraping, LLM extraction, scoring, notifications) lives in Python on Fly.io. Next.js is UI + chat orchestration only. Chat LLM calls happen in Next.js (`/api/chat`) because it owns the streaming connection to the browser; tool calls within chat reach out to Fly.io for live data.

---

## Repository Layout

```
vcmPriceAgent/
├── CLAUDE.md                        # this file
├── README.md                        # deployment guide
├── package.json                     # Next.js dependencies
├── next.config.mjs
├── tsconfig.json
├── tailwind.config.ts
├── .env.example                     # all required env vars documented
│
├── app/                             # Next.js App Router
│   ├── layout.tsx                   # nav shell
│   ├── page.tsx                     # dashboard (tickers + upcoming dividends)
│   ├── chat/page.tsx                # streaming chat UI
│   └── api/
│       ├── chat/route.ts            # LLM streaming endpoint with tool-calling
│       ├── tickers/route.ts         # proxy → Fly.io /targets
│       └── filings/route.ts         # proxy → Fly.io /dividends/upcoming
│
├── lib/
│   ├── supabase.ts                  # REPLACED by surreal.ts (todo)
│   ├── supabase-server.ts           # REPLACED by surreal-server.ts (todo)
│   ├── flyio.ts                     # typed HTTP client for Fly.io API
│   ├── llm.ts                       # DeepSeek client + system prompt
│   └── tools.ts                     # tool definitions for chat
│
├── types/index.ts                   # shared TypeScript types
│
├── supabase/migrations/             # REPLACED by surreal/schema.surql (todo)
│
├── python-service/                  # Fly.io deployment
│   ├── main.py                      # FastAPI app entry point
│   ├── fly.toml                     # Fly.io config (app: vcm-price-agent, region: sin)
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── monitor/                     # ← real source code (migrated from reference)
│   │   ├── daemon.py                # scheduler + core pipeline
│   │   ├── extractor.py             # LLM dividend classification
│   │   ├── announcement_extractor.py
│   │   ├── document_extractor.py    # PDF/HTML/Excel text extraction
│   │   ├── hkex_search.py           # direct HKEX HTTP search
│   │   ├── db.py                    # SurrealDB HTTP client
│   │   ├── notifier.py              # Slack/Discord/Telegram
│   │   ├── watchlist.py             # daily ranking orchestration
│   │   ├── history.py               # SurrealDB persistence
│   │   ├── features.py              # signal extraction for scoring
│   │   ├── scoring.py               # rule-based scoring (pure Python)
│   │   ├── registry.py              # JSON file management (targets, cache)
│   │   ├── config.py                # env + settings.json config (cached)
│   │   ├── chat.py                  # DeepSeek function-calling (internal)
│   │   ├── board_meetings.py        # HKEXnews board meeting notices
│   │   ├── activity.py              # structured event logging
│   │   ├── diagnostics.py           # error logging
│   │   └── ...
│   └── data/                        # bind-mounted volume on Fly.io
│       ├── hkex_targets.json        # watched tickers + target dates
│       ├── notified_filings.json    # dedup cache (5000-entry rolling)
│       ├── alert_history.json       # 200 most recent alerts
│       ├── settings.json            # live user config (editable via dashboard)
│       └── ...
│
└── hkex-dividend-monitor/           # reference source (read-only, do not run)
    └── monitor/                     # original Python source to port from
```

---

## Integration Checklist

### Phase 1 — Python service (Fly.io)
- [ ] Copy real `monitor/` package from `hkex-dividend-monitor/monitor/` into `python-service/monitor/`
- [ ] Merge `hkex-dividend-monitor/monitor/web.py` API routes into `python-service/main.py`
- [ ] Update `python-service/requirements.txt` from `hkex-dividend-monitor/requirements.txt`
- [ ] Wire up SurrealDB env vars (`SURREAL_ENDPOINT`, `SURREAL_NAMESPACE`, `SURREAL_DATABASE`, `SURREAL_USERNAME`, `SURREAL_PASSWORD`)
- [ ] Remove `scraper_runner.py` dependency (use `hkex_search.py` direct HTTP instead)
- [ ] Remove `bloomberg.py` / Bloomberg bridge (out of scope)
- [ ] Remove `settlement*.py` / `sgx_daily.py` (out of scope)
- [ ] Test daemon starts and polls correctly
- [ ] Test notifications fire correctly

### Phase 2 — Database (SurrealDB)
- [ ] Create Surreal Cloud project (free tier)
- [ ] Replace `supabase/migrations/001_initial_schema.sql` with `surreal/schema.surql`
  - Tables: `exchange_filing`, `company_event`, `dividend_watchlist`
  - Match schema from `hkex-dividend-monitor/monitor/db.py` and `history.py`
- [ ] Replace `lib/supabase.ts` + `lib/supabase-server.ts` with `lib/surreal.ts`
- [ ] Update `app/api/tickers/route.ts` to use SurrealDB client
- [ ] Update `.env.example` with new SurrealDB vars

### Phase 3 — Next.js frontend
- [ ] Update `lib/tools.ts` tool definitions to match real Fly.io endpoints
  - `get_filings`, `get_upcoming_dividends`, `get_watched_tickers`
  - `get_watchlist` (daily scoring), `get_alerts`, `get_board_meetings`
- [ ] Update `app/page.tsx` dashboard to show real data:
  - Active targets (ticker + target date + status)
  - Today's watchlist rankings (score, band, reasons)
  - Recent alerts feed
- [ ] Update `app/chat/page.tsx` — chat UI is largely done, verify streaming works end-to-end
- [ ] Add targets management page (`app/targets/page.tsx`)
  - Add ticker + date pairs
  - View active / inactive targets
- [ ] Add settings page (`app/settings/page.tsx`)
  - Edit poll intervals, notification webhooks, LLM model
  - Proxy POST to Fly.io `/api/settings`

### Phase 4 — Deployment
- [ ] Deploy Python service to Fly.io (`flyctl deploy` from `python-service/`)
- [ ] Set all Fly.io secrets (SurrealDB, DeepSeek, notifications, INTERNAL_SECRET)
- [ ] Deploy Next.js to Vercel (import from GitHub)
- [ ] Set all Vercel env vars (SurrealDB, DeepSeek, Fly.io URL + secret)
- [ ] Smoke test: add a ticker, trigger a poll, confirm filing appears in dashboard

---

## Environment Variables

### Next.js (Vercel)
| Variable | Description |
|---|---|
| `NEXT_PUBLIC_SURREAL_URL` | Surreal Cloud endpoint |
| `SURREAL_USER` | SurrealDB username |
| `SURREAL_PASS` | SurrealDB password |
| `SURREAL_NS` | Namespace (e.g. `hkex`) |
| `SURREAL_DB` | Database (e.g. `monitor`) |
| `FLYIO_API_URL` | `https://vcm-price-agent.fly.dev` |
| `FLYIO_API_SECRET` | Shared secret for internal calls |
| `LLM_API_KEY` | DeepSeek API key |
| `LLM_BASE_URL` | `https://api.deepseek.com` |
| `LLM_MODEL` | `deepseek-chat` |

### Python service (Fly.io secrets)
| Variable | Description |
|---|---|
| `SURREAL_ENDPOINT` | Surreal Cloud HTTP endpoint |
| `SURREAL_NAMESPACE` | e.g. `hkex` |
| `SURREAL_DATABASE` | e.g. `monitor` |
| `SURREAL_USERNAME` | SurrealDB username |
| `SURREAL_PASSWORD` | SurrealDB password |
| `LLM_API_KEY` | DeepSeek API key |
| `LLM_BASE_URL` | `https://api.deepseek.com` |
| `LLM_MODEL` | `deepseek-chat` (fast tier) |
| `LLM_REASONING_MODEL` | `deepseek-reasoner` (escalation tier) |
| `INTERNAL_SECRET` | Shared secret (validates calls from Next.js) |
| `SLACK_WEBHOOK_URL` | Optional |
| `DISCORD_WEBHOOK_URL` | Optional |
| `TELEGRAM_BOT_TOKEN` | Optional |
| `TELEGRAM_CHAT_ID` | Optional |

---

## Key Design Decisions

**SurrealDB over Supabase** — the reference codebase already uses SurrealDB (HTTP-only client in `db.py`). Adopting it avoids a rewrite of the database layer and aligns both services on the same DB.

**No scraper subprocess** — the reference code supports two HKEX ingestion paths: a subprocess CLI (`hkex-scraper`) and direct HTTP search (`hkex_search.py`). We use direct HTTP only — no external binary dependency.

**No Bloomberg** — Bloomberg bridge requires a local Bloomberg Terminal. Excluded from scope.

**No settlement prices** — SGX/Eurex settlement price modules are out of scope for this project.

**Chat LLM in Next.js, not Python** — streaming to the browser is cleaner when the LLM call originates in Next.js. Python exposes data via REST; the chat tool-calling loop lives in `app/api/chat/route.ts`.

**Config lives in Python** — `config.py` with settings.json + env var precedence is the source of truth for daemon behaviour. The Next.js settings page proxies writes to the Python service.

**Fly.io min_machines_running = 1** — the daemon must stay alive for continuous polling. Configured in `fly.toml`.
