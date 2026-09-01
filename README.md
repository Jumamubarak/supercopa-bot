# Supercopa 2027 Istanbul — Telegram Tracker Bot

A Telegram bot + scraper that monitors [tickets.rfef.es](https://tickets.rfef.es/)
for updates about the **Supercopa de España 2027** final in Istanbul
(Atatürk Olympic Stadium, February 7) and broadcasts an alert to subscribed
chats the moment anything changes — running entirely on free infrastructure,
no server or always-on laptop required.

## Architecture

Three free services, no server to maintain:

| Piece | Where it runs | What it does |
|---|---|---|
| **Telegram webhook** | Vercel serverless function (`api/webhook.py`) | Handles `/start`, `/status`, `/subscribe`, `/unsubscribe`, `/help` instantly when someone messages the bot. |
| **Scraper** | GitHub Actions cron (`.github/workflows/scrape.yml`) | Runs every 5 minutes, checks the site, and broadcasts a notification on real change. When tickets just went OPEN, it stays running in that same job and fires the full burst-alert sequence for up to an hour. |
| **Database** | Supabase (Postgres) | Stores subscriber chat IDs and the last page snapshot, shared between the two stateless pieces above. |

There is no persistent process anywhere — the webhook only runs when
Telegram calls it, and the scraper only runs on its schedule — so this
costs $0/month and needs nothing running on your machine.

## What it does

- **Bot commands:** `/start`, `/status`, `/subscribe`, `/unsubscribe`, `/help`.
- **Change detection:** hashes the normalized page text and diffs it against
  the last stored snapshot to catch subtle changes (new event cards, button
  state changes such as *"Próximamente" → "Comprar"*, price mentions).
- **Scoped to the Istanbul match specifically:** the site also hosts an
  unrelated futsal Supercopa (Palau Blaugrana). Sale status/prices/buttons
  are only read from the HTML block containing an explicit Istanbul signal
  (Estambul, Atatürk, "7 de febrero", etc.), so the futsal event's own
  "Comprar" button never triggers a false alert.
- **Resilient fetching:** tries plain `requests` first, escalates to
  `curl_cffi` (browser TLS impersonation) if it looks blocked by Cloudflare.
- **"Tickets are open" burst alert:** the instant ticket sales are detected
  as OPEN, every subscriber gets 10 notifications back to back, then a
  3-minute pause, repeating for a full hour — impossible to miss. Tunable
  via `OPEN_ALERT_BURST_COUNT` / `_INTERVAL_SECONDS` / `_DURATION_SECONDS`.
- **Persistence:** subscriber chat IDs and the latest page snapshot live in
  Supabase, so state survives every stateless invocation.

## Project layout

```
supercopa-bot/
├── lib/
│   ├── config.py         # Env var loading/validation
│   ├── db.py              # Supabase REST client (subscribers + snapshot)
│   ├── telegram_api.py    # Plain HTTP calls to the Telegram Bot API
│   └── scraper_core.py    # Fetch + BeautifulSoup parsing + diffing (sync)
├── api/
│   └── webhook.py         # Vercel function: Telegram webhook handler
├── scraper_job.py          # GitHub Actions entrypoint (periodic check + burst alert)
├── set_webhook.py           # One-off local script to point Telegram at Vercel
├── .github/workflows/scrape.yml
├── vercel.json
├── supabase_schema.sql
├── requirements.txt
├── .env.example
└── README.md
```

---

## Deployment guide (all free)

### 1. Supabase — create the database

1. Go to [supabase.com](https://supabase.com), sign up / log in, create a
   new project (pick any name/region, free tier).
2. Once it's ready: **SQL Editor → New query**, paste the contents of
   `supabase_schema.sql`, and run it. This creates the `subscribers` and
   `snapshots` tables.
3. Go to **Project Settings → API**. Note down:
   - **Project URL** → `SUPABASE_URL`
   - **service_role secret key** (not the `anon` key) → `SUPABASE_SERVICE_KEY`

### 2. Push this project to GitHub

```bash
cd ~/supercopa-bot
git init
git add .
git commit -m "Supercopa Istanbul tracker bot"
```

Create a new **public** repository on GitHub (public repos get unlimited
free GitHub Actions minutes; a private repo works too but is capped at
2,000 free minutes/month, which 5-minute polling can bump against), then:

```bash
git remote add origin https://github.com/<your-username>/<repo-name>.git
git branch -M main
git push -u origin main
```

### 3. GitHub Actions secrets

In the GitHub repo: **Settings → Secrets and variables → Actions → New
repository secret**. Add each of these:

| Secret | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Your bot token from @BotFather |
| `TELEGRAM_WEBHOOK_SECRET` | A random string you invent (e.g. `openssl rand -hex 24`) |
| `SUPABASE_URL` | From step 1 |
| `SUPABASE_SERVICE_KEY` | From step 1 |

The scrape workflow (`.github/workflows/scrape.yml`) runs every 5 minutes
automatically once these are set — no further action needed. You can also
trigger it manually from the **Actions** tab (`Run workflow`) to test it.

### 4. Vercel — deploy the webhook

1. Go to [vercel.com](https://vercel.com), sign up / log in, **Add New →
   Project**, import the GitHub repo you just pushed.
2. Vercel auto-detects the Python function in `api/webhook.py` — no build
   config needed.
3. Before deploying, add the same environment variables as the GitHub
   secrets above (**Project Settings → Environment Variables**):
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `SUPABASE_URL`,
   `SUPABASE_SERVICE_KEY`.
4. Deploy. Note the deployment URL, e.g. `https://supercopa-bot.vercel.app`.

### 5. Point Telegram at your Vercel webhook

Run this once locally (needs your `.env` filled in with the same four
values as above):

```bash
source venv/bin/activate
python set_webhook.py https://supercopa-bot.vercel.app
```

You should see `"ok": true` in the `setWebhook result` output. Test it by
messaging your bot `/start` on Telegram — it should reply within a couple
of seconds.

### 6. Subscribe

Send `/subscribe` to the bot from any chat you want notifications in.
Nobody is subscribed by default.

---

## Local development (optional)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in real values
python scraper_job.py  # runs one check-and-notify cycle, same code path as the cron job
```

There's no local server to run for the webhook — Vercel functions aren't
meant to run standalone; test webhook changes by pushing and redeploying,
or by calling the handler functions directly in a Python shell.

## Re-deploying after code changes

- **Webhook changes** (`api/`, `lib/`): just `git push` — Vercel redeploys
  automatically on every push to `main`.
- **Scraper changes** (`scraper_job.py`, `lib/`): same, GitHub Actions picks
  up the new code on the next scheduled run.
- If you rotate the bot token or webhook secret, update it in **both**
  Vercel's env vars and the GitHub Actions secrets, then re-run
  `set_webhook.py`.

## Configuration reference (`.env` / secrets / Vercel env vars)

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — (required) | Token from @BotFather |
| `TELEGRAM_WEBHOOK_SECRET` | — (required) | Random string verifying webhook calls really came from Telegram |
| `SUPABASE_URL` | — (required) | Your Supabase project URL |
| `SUPABASE_SERVICE_KEY` | — (required) | Supabase `service_role` secret key |
| `TARGET_URL` | `https://tickets.rfef.es/` | Page to monitor |
| `REQUEST_TIMEOUT_SECONDS` | `20` | HTTP timeout per request |
| `MAX_RETRIES` | `3` | Retries for transient network/5xx errors |
| `OPEN_ALERT_BURST_COUNT` | `10` | Messages sent per burst batch when tickets go OPEN |
| `OPEN_ALERT_BURST_INTERVAL_SECONDS` | `180` | Pause between burst batches |
| `OPEN_ALERT_BURST_DURATION_SECONDS` | `3600` | Total duration the burst keeps repeating |
| `OPEN_ALERT_MESSAGE_DELAY_SECONDS` | `1.5` | Delay between individual messages within one batch |

## Notes

- Notifications are only sent for genuine changes (not on the very first
  snapshot after startup) so you won't get spammed once the tables are
  first created.
- Chats that block the bot are automatically pruned from the subscriber
  list on the next broadcast attempt.
- GitHub's cron scheduler is best-effort — a "every 5 minutes" job can
  occasionally run a few minutes late under load. This is normal and not
  something to work around.
- Respect the target site's terms of service and robots.txt.
