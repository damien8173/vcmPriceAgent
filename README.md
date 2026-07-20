# HKEX Dividend Monitor

Watches Hong Kong Stock Exchange (HKEX) filings for tickers you care about, extracts dividend details with AI, and sends notifications to Slack, Discord, or Telegram. Includes a web dashboard and an AI chat interface.

## Architecture

```
┌─────────────────────────────────┐     ┌──────────────────────────────────┐
│  Vercel (Next.js)               │     │  Fly.io (Python / FastAPI)       │
│                                 │     │                                  │
│  Dashboard  ─── /api/tickers ──►│────►│  GET /tickers                    │
│  Chat UI    ─── /api/chat    ──►│     │  GET /filings?ticker=0005.HK     │
│             ─── /api/filings ──►│────►│  GET /dividends/upcoming         │
│                                 │     │  POST /poll                      │
│  /api/chat calls LLM (DeepSeek) │     │                                  │
│  LLM calls tools → Fly.io API   │     │  Scheduler: polls HKEX every 15m │
└─────────────────────────────────┘     │  Extracts dividends with AI      │
               │                        │  Sends Slack/Discord/Telegram    │
               ▼                        └────────────┬─────────────────────┘
      ┌─────────────────┐                            │
      │  Supabase       │◄───────────────────────────┘
      │  (PostgreSQL)   │
      │  tickers        │
      │  filings        │
      │  dividends      │
      │  alerts         │
      └─────────────────┘
```

## Prerequisites

- Node.js 18+
- Python 3.12+
- Accounts: [Supabase](https://supabase.com), [Vercel](https://vercel.com), [Fly.io](https://fly.io)
- API key: [DeepSeek](https://platform.deepseek.com) (or any OpenAI-compatible LLM)

---

## 1. Supabase Setup

### 1a. Create a project

1. Go to [supabase.com/dashboard](https://supabase.com/dashboard) → **New project**
2. Choose a region close to Singapore or Hong Kong
3. Save the database password somewhere safe

### 1b. Run the schema migration

1. In the Supabase dashboard, open **SQL Editor**
2. Paste the contents of `supabase/migrations/001_initial_schema.sql`
3. Click **Run**

### 1c. Get your API keys

Go to **Settings → API** and copy:
- `Project URL` → `NEXT_PUBLIC_SUPABASE_URL`
- `anon public` key → `NEXT_PUBLIC_SUPABASE_ANON_KEY`
- `service_role` key → `SUPABASE_SERVICE_ROLE_KEY` (keep this secret — server-side only)

---

## 2. Local Development

### Next.js frontend

```bash
cp .env.example .env.local
# fill in your keys in .env.local

npm install
npm run dev
# → http://localhost:3000
```

### Python service

```bash
cd python-service
cp .env.example .env
# fill in your keys in .env

python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

uvicorn main:app --reload --port 8080
# → http://localhost:8080
```

Set `FLYIO_API_URL=http://localhost:8080` in your `.env.local` to point the Next.js app at the local Python service during development.

---

## 3. Fly.io Deployment (Python service)

### 3a. Install the Fly CLI

```bash
brew install flyctl     # macOS
# or: curl -L https://fly.io/install.sh | sh
```

### 3b. Authenticate and create the app

```bash
cd python-service
flyctl auth login
flyctl launch --no-deploy   # creates the app; fly.toml is already present
```

### 3c. Set secrets

```bash
flyctl secrets set \
  SUPABASE_URL="https://<id>.supabase.co" \
  SUPABASE_SERVICE_ROLE_KEY="<key>" \
  LLM_API_KEY="<deepseek-key>" \
  INTERNAL_SECRET="<random-string>"
```

Generate a random secret with: `openssl rand -hex 32`

### 3d. Deploy

```bash
flyctl deploy
```

Your service will be live at `https://hkex-monitor-service.fly.dev`. Check `flyctl logs` if anything fails.

### 3e. Verify

```bash
curl https://hkex-monitor-service.fly.dev/health
# → {"status":"ok"}
```

---

## 4. Vercel Deployment (Next.js)

### 4a. Push to GitHub

```bash
git init
git add .
git commit -m "initial commit"
gh repo create vcm-price-agent --public --source=. --push
```

### 4b. Import in Vercel

1. Go to [vercel.com/new](https://vercel.com/new)
2. Import your GitHub repository
3. Framework: **Next.js** (auto-detected)

### 4c. Set environment variables in Vercel

In **Settings → Environment Variables**, add:

| Variable | Value |
|---|---|
| `NEXT_PUBLIC_SUPABASE_URL` | from Supabase |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | from Supabase |
| `SUPABASE_SERVICE_ROLE_KEY` | from Supabase |
| `FLYIO_API_URL` | `https://hkex-monitor-service.fly.dev` |
| `FLYIO_API_SECRET` | same random string used in Fly.io |
| `LLM_API_KEY` | DeepSeek API key |
| `LLM_BASE_URL` | `https://api.deepseek.com` |
| `LLM_MODEL` | `deepseek-chat` |

### 4d. Deploy

Click **Deploy** — Vercel builds and deploys automatically. Future pushes to `main` trigger redeploys.

---

## 5. Notification Setup (optional)

### Slack
1. Create an app at [api.slack.com/apps](https://api.slack.com/apps)
2. Enable **Incoming Webhooks** and add a webhook to a channel
3. Add the URL as `SLACK_WEBHOOK_URL` in Fly.io secrets

### Discord
1. In your server, go to **Channel Settings → Integrations → Webhooks**
2. Create a webhook and copy the URL
3. Add as `DISCORD_WEBHOOK_URL` in Fly.io secrets

### Telegram
1. Message [@BotFather](https://t.me/BotFather) → `/newbot`
2. Copy the bot token
3. Start a chat with your bot, then fetch your chat ID:
   `https://api.telegram.org/bot<token>/getUpdates`
4. Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` to Fly.io secrets

---

## 6. Adding Watched Tickers

Use the dashboard at your Vercel URL, or via the API directly:

```bash
curl -X POST https://<your-app>.vercel.app/api/tickers \
  -H "Content-Type: application/json" \
  -d '{"symbol": "0005.HK", "name": "HSBC Holdings"}'
```

---

## Project Structure

```
vcmPriceAgent/
├── app/                    # Next.js App Router
│   ├── page.tsx            # Dashboard
│   ├── chat/page.tsx       # Chat interface
│   └── api/
│       ├── chat/route.ts   # LLM streaming endpoint
│       ├── tickers/route.ts
│       └── filings/route.ts
├── lib/
│   ├── supabase.ts         # Browser Supabase client
│   ├── supabase-server.ts  # Server-side Supabase client
│   ├── flyio.ts            # Fly.io API wrapper
│   ├── llm.ts              # LLM client (DeepSeek)
│   └── tools.ts            # Tool definitions for chat
├── types/index.ts          # Shared TypeScript types
├── supabase/migrations/    # SQL schema
└── python-service/         # Fly.io Python backend
    ├── main.py             # FastAPI app + scheduler
    ├── monitor/
    │   ├── hkex.py         # HKEX filing scraper
    │   └── extractor.py    # AI dividend extraction
    ├── Dockerfile
    └── fly.toml
```
