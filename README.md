# HKEX Dividend Monitor

Watches Hong Kong Stock Exchange (HKEX) filings for tickers you care about, extracts dividend details with AI, and sends notifications to Slack, Discord, or Telegram. Includes a web dashboard and an AI chat interface.

## Architecture

```
┌─────────────────────────────────┐     ┌──────────────────────────────────┐
│  Vercel (Next.js)               │     │  Fly.io (Python / FastAPI)       │
│                                 │     │                                  │
│  Dashboard  ─── /api/tickers ──►│────►│  GET /targets                    │
│  Chat UI    ─── /api/chat    ──►│     │  GET /filings                    │
│             ─── /api/filings ──►│────►│  GET /dividends/upcoming         │
│                                 │     │  POST /poll                      │
│  /api/chat calls DeepSeek LLM   │     │                                  │
│  LLM tool calls → Fly.io API    │     │  Daemon: polls HKEX every 3 min  │
└─────────────────────────────────┘     │  Extracts dividends with AI      │
               │                        │  Sends Slack/Discord/Telegram    │
               ▼                        └────────────┬─────────────────────┘
      ┌─────────────────┐                            │
      │  Supabase        │◄───────────────────────────┘
      │  (PostgreSQL)    │
      │  exchange_filing │
      │  company_event   │
      │  dividend_watchlist│
      │  alert_history   │
      └─────────────────┘
```

## Prerequisites

- Node.js 18+
- Python 3.12+
- Accounts: [Supabase](https://supabase.com), [Vercel](https://vercel.com), [Fly.io](https://fly.io)
- API key: [DeepSeek](https://platform.deepseek.com)

---

## 1. Supabase Setup

### 1a. Create a project

1. Go to [supabase.com/dashboard](https://supabase.com/dashboard) → **New project**
2. Choose **Singapore** region (closest to Hong Kong)
3. Save the database password — you'll need it for the GitHub Actions secret

### 1b. Run the initial schema

1. In the Supabase dashboard → **SQL Editor**
2. Paste the contents of `supabase/migrations/001_initial_schema.sql`
3. Click **Run**

### 1c. Get your API keys

Go to **Settings → API** and copy:

| Key | Used for |
|---|---|
| Project URL | `NEXT_PUBLIC_SUPABASE_URL` and `SUPABASE_URL` |
| `anon` / public key | `NEXT_PUBLIC_SUPABASE_ANON_KEY` |
| `service_role` key | `SUPABASE_SERVICE_ROLE_KEY` — server-side only, never expose to browser |

Get your **Project ID** from Settings → General (the reference ID, e.g. `abcdefghijklmnop`).

---

## 2. Supabase GitHub Integration (auto-migrations)

Every time you push a new file to `supabase/migrations/`, the GitHub Action automatically applies it to your Supabase project. You never need to run SQL manually again.

### 2a. Get a Supabase access token

1. Go to [supabase.com/dashboard/account/tokens](https://supabase.com/dashboard/account/tokens)
2. Click **Generate new token** → name it `github-actions` → copy it

### 2b. Add GitHub repo secrets

Go to your GitHub repo → **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret name | Value |
|---|---|
| `SUPABASE_ACCESS_TOKEN` | Token from step 2a |
| `SUPABASE_PROJECT_ID` | Your project reference ID (e.g. `abcdefghijklmnop`) |
| `SUPABASE_DB_PASSWORD` | Database password from project creation |

### 2c. How to add a new migration

Create a new numbered file in `supabase/migrations/`:

```bash
# Example: add a new column
touch supabase/migrations/002_add_ticker_notes.sql
```

Write your SQL, then commit and push to `main`:

```bash
git add supabase/migrations/002_add_ticker_notes.sql
git commit -m "Add notes column to targets"
git push
```

The GitHub Action runs automatically and applies the migration. Check progress in **Actions** tab on GitHub.

> **Rule:** Never edit the schema directly in the Supabase dashboard. All changes go through migration files so the schema stays in sync with the code.

---

## 3. Local Development

### Next.js frontend

```bash
cp .env.example .env.local
# fill in your Supabase and DeepSeek keys

npm install
npm run dev
# → http://localhost:3000
```

### Python service

```bash
cd python-service
cp .env.example .env
# fill in your keys

python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

uvicorn main:app --reload --port 8080
# → http://localhost:8080
```

Set `FLYIO_API_URL=http://localhost:8080` in your `.env.local` to point Next.js at the local Python service.

---

## 4. Fly.io Deployment (Python service)

### 4a. Install the Fly CLI

```bash
brew install flyctl          # macOS
# or: curl -L https://fly.io/install.sh | sh
```

### 4b. Sign up and authenticate

```bash
flyctl auth signup           # new account
# or
flyctl auth login            # existing account
```

A browser window opens for authentication. Fly.io requires a credit card on file but stays within free limits for this workload (1 shared-CPU VM, 256MB RAM).

### 4c. Create the app

```bash
cd python-service
flyctl launch --no-deploy
```

When prompted:
- **App name:** `vcm-price-agent` (or accept generated name)
- **Region:** `sin` (Singapore) — closest to Hong Kong
- **Postgres / Redis:** No (we use Supabase)

This creates the app on Fly.io. The `fly.toml` is already configured.

### 4d. Set secrets

Generate a random internal secret first:

```bash
openssl rand -hex 32
# → copy the output, use as INTERNAL_SECRET below
```

Set all secrets in one command:

```bash
flyctl secrets set \
  SUPABASE_URL="https://<project-id>.supabase.co" \
  SUPABASE_SERVICE_ROLE_KEY="<service-role-key>" \
  LLM_API_KEY="<deepseek-api-key>" \
  LLM_BASE_URL="https://api.deepseek.com" \
  LLM_MODEL="deepseek-chat" \
  LLM_REASONING_MODEL="deepseek-reasoner" \
  INTERNAL_SECRET="<your-generated-secret>"
```

Optionally add notification webhooks:

```bash
flyctl secrets set \
  SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..." \
  DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..." \
  TELEGRAM_BOT_TOKEN="<token>" \
  TELEGRAM_CHAT_ID="<chat-id>"
```

### 4e. Deploy

```bash
flyctl deploy
```

First deploy takes ~3 minutes (builds Docker image). Subsequent deploys are faster.

### 4f. Verify

```bash
flyctl status               # check machine is running
flyctl logs                 # tail live logs
curl https://vcm-price-agent.fly.dev/health
# → {"status":"ok"}
```

### 4g. Useful Fly.io commands

```bash
flyctl logs                  # live log stream
flyctl status                # machine health
flyctl ssh console           # shell into the running container
flyctl secrets list          # list secret names (not values)
flyctl deploy                # redeploy after code changes
flyctl scale show            # check VM size and count
```

### 4h. Keeping the machine alive

The `fly.toml` sets `min_machines_running = 1` and `auto_stop_machines = false`. This ensures the polling daemon stays running continuously. Fly.io free tier includes enough allowance for one always-on small VM.

---

## 5. Vercel Deployment (Next.js)

### 5a. Push to GitHub

```bash
git add .
git commit -m "ready to deploy"
git push
```

### 5b. Import in Vercel

1. Go to [vercel.com/new](https://vercel.com/new)
2. Click **Import** next to your `vcmPriceAgent` repository
3. Framework preset: **Next.js** (auto-detected)
4. Click **Deploy** — first deploy will fail because env vars aren't set yet, that's fine

### 5c. Set environment variables

In Vercel → your project → **Settings → Environment Variables**, add:

| Variable | Value |
|---|---|
| `NEXT_PUBLIC_SUPABASE_URL` | `https://<project-id>.supabase.co` |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | anon key from Supabase |
| `SUPABASE_SERVICE_ROLE_KEY` | service role key from Supabase |
| `FLYIO_API_URL` | `https://vcm-price-agent.fly.dev` |
| `FLYIO_API_SECRET` | same value as Fly.io `INTERNAL_SECRET` |
| `LLM_API_KEY` | DeepSeek API key |
| `LLM_BASE_URL` | `https://api.deepseek.com` |
| `LLM_MODEL` | `deepseek-chat` |

### 5d. Redeploy

In Vercel → **Deployments** → click the three dots on the latest deployment → **Redeploy**.

Future pushes to `main` trigger automatic redeployments.

---

## 6. Notification Setup (optional)

### Slack
1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App → From scratch**
2. Enable **Incoming Webhooks** → **Add New Webhook to Workspace** → choose a channel
3. Copy the webhook URL → add as `SLACK_WEBHOOK_URL` in Fly.io secrets

### Discord
1. In your Discord server → channel **Settings → Integrations → Webhooks → New Webhook**
2. Copy the URL → add as `DISCORD_WEBHOOK_URL` in Fly.io secrets

### Telegram
1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → follow prompts → copy the bot token
2. Start a conversation with your bot, then get your chat ID:
   ```
   https://api.telegram.org/bot<token>/getUpdates
   ```
   Look for `"chat":{"id":...}` in the response
3. Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` to Fly.io secrets

---

## 7. Adding Watched Targets

Use the dashboard, or via the API:

```bash
# Add a ticker with a target ex-date
curl -X POST https://<your-app>.vercel.app/api/tickers \
  -H "Content-Type: application/json" \
  -d '{"symbol": "0005.HK", "name": "HSBC Holdings", "target_date": "2026-08-01"}'
```

---

## Project Structure

```
vcmPriceAgent/
├── app/                    # Next.js App Router
│   ├── page.tsx            # Dashboard
│   ├── chat/page.tsx       # Chat interface
│   └── api/
│       ├── chat/route.ts   # LLM streaming + tool-calling
│       ├── tickers/route.ts
│       └── filings/route.ts
├── lib/
│   ├── supabase.ts         # Browser Supabase client
│   ├── supabase-server.ts  # Server-side Supabase client
│   ├── flyio.ts            # Fly.io API wrapper
│   ├── llm.ts              # DeepSeek LLM client
│   └── tools.ts            # Chat tool definitions
├── types/index.ts
├── supabase/migrations/    # SQL schema — edit here, auto-applied via GitHub Actions
├── .github/workflows/
│   └── supabase-migrations.yml
└── python-service/         # Fly.io Python backend
    ├── main.py
    ├── monitor/            # HKEX monitoring logic
    ├── Dockerfile
    └── fly.toml
```
