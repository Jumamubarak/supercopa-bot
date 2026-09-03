# Supercopa 2027 Istanbul & Yeezy — Telegram Tracker Bot

A Telegram bot + scraper that monitors two unrelated sites and broadcasts
an alert to subscribed chats the moment anything changes — running
entirely on free infrastructure, no server or always-on laptop required:

- [tickets.rfef.es](https://tickets.rfef.es/) — ticket sales for the
  **Supercopa de España 2027** final in Istanbul (Atatürk Olympic Stadium,
  February 7).
- [yeezy.com](https://yeezy.com) — new product drops in the Yeezy store.

## Architecture

Three free services, no server to maintain:

| Piece | Where it runs | What it does |
|---|---|---|
| **Telegram webhook** | Vercel serverless function (`api/webhook.py`) | Handles `/start`, `/status`, `/status_supercopa`, `/status_yeezy`, `/subscribe`, `/unsubscribe`, `/help` instantly when someone messages the bot. |
| **Scrapers** | GitHub Actions cron (`.github/workflows/scrape_supercopa.yml`, `scrape_yeezy.yml`) | Two independent schedules (every 5 / 10 minutes) each check one site and broadcast a notification on real change. When Supercopa tickets just went OPEN, that job stays running and fires the full burst-alert sequence for up to an hour. |
| **Database** | Supabase (Postgres) | Stores subscriber chat IDs, the Supercopa page snapshot, and known Yeezy product IDs, shared between the stateless pieces above. |

There is no persistent process anywhere — the webhook only runs when
Telegram calls it, and each scraper only runs on its own schedule — so this
costs $0/month and needs nothing running on your machine.

Each monitor (`lib/monitors/supercopa.py`, `lib/monitors/yeezy.py`) is
isolated behind `lib/monitors/base.py`'s `safe_check()`: one site failing
(blocked request, layout change) can never take down the other's checks or
the bot's command handling.

## What it does

- **Bot commands:** `/start`, `/status` (both sites combined),
  `/status_supercopa`, `/status_yeezy`, `/subscribe`, `/unsubscribe`, `/help`.
- **Supercopa change detection:** hashes the normalized page text and diffs
  it against the last stored snapshot to catch subtle changes (new event
  cards, button state changes such as *"Próximamente" → "Comprar"*, price
  mentions). Scoped specifically to the Istanbul match — the site also
  hosts an unrelated futsal Supercopa, whose own "Comprar" button never
  triggers a false alert.
- **Yeezy new-drop detection:** yeezy.com isn't Shopify — it's a custom
  SvelteKit/Swell storefront with no public products API. The full catalog
  is embedded server-side in the homepage HTML (SvelteKit's hydration
  payload), so a plain synchronous fetch is enough — no headless browser
  needed. New product IDs (`pId`, stable across colorways/sizes) trigger a
  photo+caption alert with title, price, and a link to the store.
- **Resilient fetching:** tries plain `requests` first, escalates to
  `curl_cffi` (browser TLS impersonation) if it looks blocked by Cloudflare
  — shared by both monitors.
- **"Tickets are open" burst alert:** the instant Supercopa ticket sales
  are detected as OPEN, every subscriber gets 10 notifications back to
  back, then a 3-minute pause, repeating for a full hour — impossible to
  miss. Tunable via `OPEN_ALERT_BURST_COUNT` / `_INTERVAL_SECONDS` /
  `_DURATION_SECONDS`.
- **Persistence:** subscriber chat IDs, the Supercopa snapshot, and known
  Yeezy product IDs live in Supabase, so state survives every stateless
  invocation.

## Project layout

```
supercopa-bot/
├── lib/
│   ├── config.py             # Env var loading/validation
│   ├── db.py                  # Supabase REST client (subscribers + snapshot + yeezy_products)
│   ├── telegram_api.py        # Plain HTTP calls to the Telegram Bot API (text + photo)
│   ├── scraper_core.py        # Fetch + BeautifulSoup parsing + diffing for Supercopa (sync)
│   └── monitors/
│       ├── base.py             # BaseMonitor interface + safe_check() failure isolation
│       ├── supercopa.py        # SupercopaMonitor
│       └── yeezy.py            # YeezyMonitor (embedded-catalog extraction)
├── api/
│   └── webhook.py          # Vercel function: Telegram webhook handler
├── scraper_job.py           # GitHub Actions entrypoint: `python scraper_job.py supercopa|yeezy|all`
├── bot.py                    # LOCAL DEV ONLY: standalone aiogram bot + asyncio background loops
├── set_webhook.py             # One-off local script to point Telegram at Vercel
├── .github/workflows/scrape_supercopa.yml
├── .github/workflows/scrape_yeezy.yml
├── vercel.json
├── supabase_schema.sql
├── requirements.txt           # Vercel/GitHub Actions dependencies (no aiogram)
├── requirements-bot.txt       # Extra dependency (aiogram) for bot.py only
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

The two scrape workflows (`.github/workflows/scrape_supercopa.yml` every 5
minutes, `scrape_yeezy.yml` every 10 minutes) run automatically once these
are set — no further action needed. You can also trigger either manually
from the **Actions** tab (`Run workflow`) to test it. You'll also need to
run `supabase_schema.sql`'s `yeezy_products` table creation if you didn't
apply the whole file in step 1.

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
cp .env.example .env       # fill in real values
python scraper_job.py all  # runs one check-and-notify cycle for both sites, same code path as the cron jobs
python scraper_job.py supercopa  # or just one
python scraper_job.py yeezy
```

There's no local server to run for the webhook — Vercel functions aren't
meant to run standalone; test webhook changes by pushing and redeploying,
or by calling the handler functions directly in a Python shell.

### Standalone bot.py (optional, local dev only)

If you'd rather run a classic always-on bot locally (long-polling +
`asyncio` background loops checking each site on its own interval) instead
of the Vercel/GitHub Actions split, `bot.py` provides that:

```bash
pip install -r requirements.txt -r requirements-bot.txt
python bot.py
```

This is **not** what runs in production and does need your machine on the
whole time it's running — it exists for local testing/iteration. It shares
all the same monitor/db/telegram code as the deployed version.

## Re-deploying after code changes

- **Webhook changes** (`api/`, `lib/`): just `git push` — Vercel redeploys
  automatically on every push to `main`.
- **Scraper changes** (`scraper_job.py`, `lib/`): same, GitHub Actions picks
  up the new code on the next scheduled run of either workflow.
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
| `TARGET_URL` | `https://tickets.rfef.es/` | Supercopa page to monitor |
| `YEEZY_TARGET_URL` | `https://yeezy.com` | Yeezy store to monitor |
| `REQUEST_TIMEOUT_SECONDS` | `20` | HTTP timeout per request |
| `MAX_RETRIES` | `3` | Retries for transient network/5xx errors |
| `OPEN_ALERT_BURST_COUNT` | `10` | Messages sent per burst batch when tickets go OPEN |
| `OPEN_ALERT_BURST_INTERVAL_SECONDS` | `180` | Pause between burst batches |
| `OPEN_ALERT_BURST_DURATION_SECONDS` | `3600` | Total duration the burst keeps repeating |
| `OPEN_ALERT_MESSAGE_DELAY_SECONDS` | `1.5` | Delay between individual messages within one batch |
| `SUPERCOPA_CHECK_INTERVAL` | `300` | Seconds between checks — used by `bot.py`'s background loop; production cadence is set by `scrape_supercopa.yml`'s cron |
| `YEEZY_CHECK_INTERVAL` | `600` | Seconds between checks — used by `bot.py`'s background loop; production cadence is set by `scrape_yeezy.yml`'s cron |

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
