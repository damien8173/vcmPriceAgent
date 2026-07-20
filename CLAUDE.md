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

Supabase (managed PostgreSQL)
  ├── exchange_filing     — raw HKEX filings cache
  ├── company_event       — LLM-extracted facts (board meetings, dividends, results)
  ├── dividend_watchlist  — daily scored rankings
  └── alert_history       — notification dispatch log
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
├── .github/
│   └── workflows/
│       └── supabase-migrations.yml  # auto-apply migrations on push to main
│
├── app/                             # Next.js App Router
│   ├── layout.tsx                   # nav shell
│   ├── page.tsx                     # dashboard (targets + watchlist + alerts)
│   ├── chat/page.tsx                # streaming chat UI
│   └── api/
│       ├── chat/route.ts            # LLM streaming endpoint with tool-calling
│       ├── tickers/route.ts         # proxy → Fly.io /targets
│       └── filings/route.ts         # proxy → Fly.io /dividends/upcoming
│
├── lib/
│   ├── supabase.ts                  # browser Supabase client
│   ├── supabase-server.ts           # server-side Supabase client (API routes)
│   ├── flyio.ts                     # typed HTTP client for Fly.io API
│   ├── llm.ts                       # DeepSeek client + system prompt
│   └── tools.ts                     # tool definitions for chat
│
├── types/index.ts                   # shared TypeScript types
│
├── supabase/
│   └── migrations/
│       └── 001_initial_schema.sql   # full schema — edit here, auto-applied via CI
│
├── python-service/                  # Fly.io deployment
│   ├── main.py                      # FastAPI app entry point + scheduler
│   ├── fly.toml                     # app: vcm-price-agent, region: sin, min 1 machine
│   ├── Dockerfile
│   ├── requirements.txt
│   └── monitor/                     # ← ported from hkex-dividend-monitor/monitor/
│       ├── daemon.py                # scheduler + core polling pipeline
│       ├── extractor.py             # LLM dividend classification (two-tier)
│       ├── announcement_extractor.py
│       ├── document_extractor.py    # PDF/HTML/Excel text extraction
│       ├── hkex_search.py           # direct HKEX HTTP search
│       ├── db.py                    # Supabase client (replaces SurrealDB original)
│       ├── notifier.py              # Slack/Discord/Telegram dispatch
│       ├── watchlist.py             # daily ranking orchestration
│       ├── history.py               # Supabase persistence layer
│       ├── features.py              # signal extraction for scoring
│       ├── scoring.py               # rule-based scoring (pure Python, no LLM)
│       ├── registry.py              # JSON file management (targets, dedup cache)
│       ├── config.py                # env + settings.json config (mtime-cached)
│       ├── board_meetings.py        # HKEXnews board meeting notices
│       ├── activity.py              # structured event logging
│       ├── diagnostics.py           # error logging
│       └── ...
│
└── hkex-dividend-monitor/           # reference source (read-only, do not run)
    └── monitor/                     # original Python to port from
```

---

## Integration Checklist

### Phase 1 — Python service (Fly.io)
- [ ] Copy `monitor/` package from `hkex-dividend-monitor/monitor/` into `python-service/monitor/`
- [ ] Rewrite `db.py` to use `supabase-py` instead of SurrealDB HTTP client
- [ ] Rewrite `history.py` persistence calls to use Supabase (PostgREST / raw SQL)
- [ ] Merge `web.py` API routes into `python-service/main.py`
- [ ] Update `requirements.txt` (base on `hkex-dividend-monitor/requirements.txt`, swap `surrealdb` → `supabase`)
- [ ] Remove `scraper_runner.py` dependency (use `hkex_search.py` direct HTTP only)
- [ ] Remove `bloomberg.py` (out of scope — requires Bloomberg Terminal)
- [ ] Remove `settlement*.py` / `sgx_daily.py` (out of scope)
- [ ] Test daemon starts and polls HKEX correctly
- [ ] Test LLM extraction pipeline works end-to-end
- [ ] Test notifications fire on a matched filing

### Phase 2 — Database (Supabase)
- [ ] Create Supabase project (free tier, Singapore region)
- [ ] Run `supabase/migrations/001_initial_schema.sql` via SQL Editor
- [ ] Add `SUPABASE_ACCESS_TOKEN` + `SUPABASE_PROJECT_ID` as GitHub repo secrets
- [ ] Verify GitHub Actions workflow auto-applies future migrations on push
- [ ] Update Supabase env vars in both Vercel and Fly.io

### Phase 3 — Next.js frontend
- [ ] Update `lib/tools.ts` definitions to match real Fly.io endpoints
- [ ] Update `app/page.tsx` dashboard — active targets, watchlist rankings, recent alerts
- [ ] Add `app/targets/page.tsx` — add/remove ticker+date watchlist targets
- [ ] Add `app/settings/page.tsx` — edit notification webhooks, poll intervals (proxies to Fly.io)
- [ ] Verify chat streaming works end-to-end with real tool calls

### Phase 4 — Deployment
- [ ] Deploy Python service: `flyctl deploy` from `python-service/`
- [ ] Set all Fly.io secrets (Supabase, DeepSeek, notifications, INTERNAL_SECRET)
- [ ] Deploy Next.js: import repo in Vercel dashboard
- [ ] Set all Vercel env vars
- [ ] Smoke test: add a ticker+date target → trigger poll → confirm filing in dashboard → confirm notification sent

---

## Environment Variables

### Next.js (Vercel)
| Variable | Description |
|---|---|
| `NEXT_PUBLIC_SUPABASE_URL` | `https://<project-id>.supabase.co` |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | Supabase anon/public key |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase service role key (server-side only) |
| `FLYIO_API_URL` | `https://vcm-price-agent.fly.dev` |
| `FLYIO_API_SECRET` | Shared secret — must match Fly.io `INTERNAL_SECRET` |
| `LLM_API_KEY` | DeepSeek API key |
| `LLM_BASE_URL` | `https://api.deepseek.com` |
| `LLM_MODEL` | `deepseek-chat` |

### Python service (Fly.io secrets)
| Variable | Description |
|---|---|
| `SUPABASE_URL` | `https://<project-id>.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase service role key |
| `LLM_API_KEY` | DeepSeek API key |
| `LLM_BASE_URL` | `https://api.deepseek.com` |
| `LLM_MODEL` | `deepseek-chat` (fast tier) |
| `LLM_REASONING_MODEL` | `deepseek-reasoner` (escalation tier) |
| `INTERNAL_SECRET` | Validates calls from Next.js — generate with `openssl rand -hex 32` |
| `SLACK_WEBHOOK_URL` | Optional |
| `DISCORD_WEBHOOK_URL` | Optional |
| `TELEGRAM_BOT_TOKEN` | Optional |
| `TELEGRAM_CHAT_ID` | Optional |

### GitHub repo secrets (for Supabase CI)
| Secret | Description |
|---|---|
| `SUPABASE_ACCESS_TOKEN` | From supabase.com/dashboard/account/tokens |
| `SUPABASE_PROJECT_ID` | Project reference ID (Settings → General) |
| `SUPABASE_DB_PASSWORD` | Database password set at project creation |

---

## Key Design Decisions

**Supabase over SurrealDB** — managed PostgreSQL with a polished dashboard, proven JS and Python SDKs, and a free tier generous enough for this workload. The reference code used SurrealDB but its `db.py` is an isolated module — swapping it for `supabase-py` is contained.

**db.py is the only file that changes for the DB swap** — all other monitor modules call functions in `db.py` and `history.py`. Rewriting those two files is the full scope of the database migration.

**No scraper subprocess** — the reference code supports two HKEX ingestion paths: a subprocess CLI (`hkex-scraper`) and direct HTTP search (`hkex_search.py`). We use direct HTTP only — no external binary dependency.

**No Bloomberg** — Bloomberg bridge requires a local Bloomberg Terminal. Excluded from scope.

**No settlement prices** — SGX/Eurex settlement price modules are out of scope.

**Chat LLM in Next.js, not Python** — streaming to the browser is cleaner when the LLM call originates in Next.js. Python exposes data via REST; the chat tool-calling loop lives in `app/api/chat/route.ts`.

**Config source of truth is Python** — `config.py` with `settings.json` + env var precedence controls daemon behaviour. The Next.js settings page proxies writes to Fly.io `/api/settings`.

**Fly.io `min_machines_running = 1`** — the daemon must stay alive for continuous polling. Configured in `fly.toml`.

**Migrations are code** — all schema changes go in `supabase/migrations/` and are applied automatically via GitHub Actions on push to `main`. Never edit the schema manually in the Supabase dashboard.
