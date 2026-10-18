# Setup guide: getting your keys

Put every value in a local `.env` file (copy `.env.example`). Never paste secrets into chat, commits, or screenshots.

## 1. Anthropic API key (`ANTHROPIC_API_KEY`)
1. Go to https://console.anthropic.com and sign in (this is separate from a claude.ai subscription).
2. Settings -> Billing: add a card and prepay a small amount ($10-20 is plenty to start).
3. Settings -> API keys -> Create key. Name it `zypher-sales-agent`. Copy it immediately (shown once).
4. In Settings -> Limits, set a monthly spend limit (e.g. $20) as a safety net.
5. `.env`: `ANTHROPIC_API_KEY=sk-ant-...`

## 2. Supabase database (`DATABASE_URL`)
1. Go to https://supabase.com -> New project. Pick a region near you (Mumbai/Singapore). Save the database password you set.
2. Open the project -> Connect (top bar) -> choose **Session pooler** URI (works on IPv4 networks; the direct connection is IPv6-only on the free tier).
3. Copy the URI and replace `[YOUR-PASSWORD]` with your database password. If the password has special characters (`@ : / #`), URL-encode them.
4. `.env`: `DATABASE_URL=postgresql://postgres.xxxx:PASSWORD@aws-0-REGION.pooler.supabase.com:5432/postgres`
5. Create the tables: SQL Editor -> New query -> paste `schema.sql` -> Run. That file is the complete current schema (it already includes every migration in `migrations/`), so on a fresh project run **only** `schema.sql`. The numbered files in `migrations/` are for upgrading an older database.
6. Never use the `service_role` or `anon` API keys for this; we connect straight to Postgres.

## 3. Telegram approval bot (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS`)
1. In Telegram, open **@BotFather** -> send `/newbot` -> choose a name and a username ending in `bot`. It replies with the token.
2. Open your new bot and press **Start** (a bot cannot message you until you do).
3. Get your numeric user ID: message **@userinfobot**. For a partner, they do the same; separate IDs with commas.
4. `.env`:
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC...
   TELEGRAM_ALLOWED_USER_IDS=111111111,222222222
   ```

## 4. Lead list
CSV with company, website, email, country, contact name. See `scripts/import_leads.py` for exact columns. Start with 20-30 real leads you have hand-checked.

## 5. Check it works
```
pip install -e ".[dev]"
python -m pytest
python -m zypher_agent --help
```
Start in dry-run mode first (`dry_run.py`) so nothing is sent while you inspect drafts.

## Not needed yet
SMTP/sending domain (Phase 2), Slack, Notion, WhatsApp, Apollo, payment providers. Skip these until we get there; leave their `.env` lines empty.

## When we reach sending (Phase 2) you will need
1. A separate domain for cold email (e.g. `zypher-mail.com`), not your main one. Any registrar (Namecheap, GoDaddy, Cloudflare).
2. 1-2 mailboxes on it (Google Workspace, about $6/user/month), with SPF, DKIM, DMARC configured.
3. An app password or SMTP credentials for those mailboxes.
4. The LLP's registered postal address for the email footer.
5. 2-3 weeks of warmup before real volume.
