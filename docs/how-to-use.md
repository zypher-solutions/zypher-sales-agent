# How to use the sales agent (current version)

You never need Claude to run this day to day. Open a terminal in the project folder and use `.\zypher.ps1`.
Phase 2 (sending, follow-ups, bounces, stats) is built, and the agent can now run on a server in Docker (see "Running on the server" below). Not built yet: writing replies for you, CRM handoff, LinkedIn and WhatsApp, and counting meetings booked.

## One-time setup (already done on this PC)
1. `.env` holds your keys (Groq, Apify, Telegram, database) and your sender details. Never share or commit it.
2. Supabase has the tables (`schema.sql`).
3. Telegram: your bot is `@zypher_sales_bot`; only the user ids in `TELEGRAM_ALLOWED_USER_IDS` can use it.

## The simple way to run it
Open one terminal in the project folder and run `.\zypher.ps1 start`. Leave the window open. It runs the Telegram buttons, the sending, the inbox reading and the follow-ups together. In Telegram: when you tap **Approve**, it asks "Yes, send to <address>?" and only the second tap approves. The card then tells you when the email goes out. To stop everything press Ctrl+C in that window. To update after code changes, stop it and start it again.

## Daily routine (about 10 minutes)

| Step | Command | What it does | Cost |
|---|---|---|---|
| 1 | `.\zypher.ps1 status` | Shows how many leads are at each stage and what needs you | free |
| 2 | `.\zypher.ps1 find "trading company" "Dubai, United Arab Emirates" 20` (see "What to search for" below) | Finds companies on Google Maps that have their own website, adds them as new leads. Run it again with a different search or city whenever you need more leads. Re-running never duplicates anyone | about $0.004 per company (Apify free credit is about $5/month, so roughly 1,000 companies) |
| 3 | `.\zypher.ps1 draft 5` | Reads each company's website, finds a public business email, writes the email, checks it against the rules, sends a card to Telegram. It prints a line for each company as it goes. About a minute per lead on the free model (slower when the free model's per-minute limit is hit) | free |
| 4 | `.\zypher.ps1 start` (leave the window open) | Runs everything at once: your Telegram buttons, sending, inbox reading and follow-ups | free |
| 5 | Telegram | Review each card, then tap Approve, Suggest changes or Reject | |

## Who we are looking for
A kind of company, not a sector: an established business with a proper office and an office team, formal quotations, contracts and invoices, and a flow that is real but messy. Not freelancers, one-person or informal businesses, or walk-in shops. The research step checks this on each website and rejects the rest with a reason.

## The "Why they fit" line
Under the evidence on each card, a line starting with 🏢 quotes the sentence from the company's own website that shows it has a real office team or sells to businesses. Companies that show no such sentence are rejected before an email is written. A rejected company is deleted from the database (contacts, research, drafts); only its website and the reason are kept, so `find` never adds it again and you never pay to research it twice. `status` counts them as rejected. A company you have already emailed is never deleted, because its history is needed for replies and opt-outs. If a company's website blocks automatic visits or is down, that is not a rejection: the lead is kept and parked for a call or LinkedIn (`status` shows it under "call or LinkedIn by hand"). If a card's "Why they fit" line looks weak to you, reject the card; this is where your judgement matters most.

## What to search for
Google Maps cannot filter for "has a proper office team", so searches are broad and the research step does the filtering. Use words for business-to-business companies rather than shops: "trading company", "supplier", "distributor", "importer", "contracting company", "engineering company", "consultancy", "manufacturer", "wholesaler", "fit out company". Avoid shop words like "store" or "salon". Use `--preview` to look at the results for pennies before importing, and look at which companies would pass the "proper office, proper paperwork" test.

## Reading a Telegram card
- Everything between **THE EMAIL** and **END OF EMAIL** is exactly what the lead would receive (subject, greeting, body, your signature, opt-out line, address).
- Below that, **ONLY FOR YOU, NOT SENT** shows the evidence: the exact sentences from their website that the email is based on. Click the link to check any claim.
- **To:** is the public business address found on their own site. **Phone:** is for you to call or WhatsApp by hand.
- A green tick means every automatic check passed. A red warning lists what failed, and Approve is disabled.

## The buttons
- **Approve**: asks "Yes, send to <address>?". Tap Yes to approve (Cancel changes nothing). The sender emails it at the next allowed moment, and the card says when.
- **Suggest changes**: then type what you want ("shorter", "mention the customs paperwork"). A new version is written and the old card stops working.
- **Reject**: this draft was not good. The company stays in the list and a fresh draft is written next time you run `draft`. To never contact a company again, type `/suppress theirdomain.com` in the chat.

## Chat commands
- `/suppress name@company.com` never contact this address
- `/suppress company.com` never contact anyone at this company

Anyone who replies "no thanks" should be added with `/suppress` straight away.

## Your signature
Set in `.env`: `SENDER_NAME`, `SENDER_TITLE`, `SENDER_EMAIL`, `SENDER_PHONE`, `SENDER_WEBSITE`, `SENDER_POSTAL_ADDRESS`, `SENDER_DISCLAIMER`, `SENDER_SIGN_OFF` (default "Best regards,") and `SENDER_LOGO_PATH` (a small PNG, e.g. `assets/logo.png`). Every email ends the same way: sign-off, name and title, company, Email | Office | Website, address, logo, a thin line, then the opt-out line and your confidentiality notice. The AI only writes the body; the signature is always added by code. `.\zypher.ps1 preflight` warns if the logo file is missing or too big. After changing it, run `.\zypher.ps1 refresh` so drafts that are still waiting use the new signature.

## Sending (switched off until you turn it on)
1. Put `MAIL_PASSWORD=` (your mailbox password) in `.env` yourself. Never paste it anywhere else.
2. `.\zypher.ps1 preflight` checks DNS, SPF, DMARC and logs in to your mailbox. It sends nothing. Fix every FAIL first.
3. For the first test, set `SEND_ALLOWLIST=` to your own other email addresses (for example your Gmail) and `SENDING_ENABLED=true`. Only those can receive mail.
4. Run `.\zypher.ps1 start` and leave it open. Approve a draft in Telegram (tap Approve, then Yes), and the card tells you when it goes out. (`.\zypher.ps1 send` does one sending pass by hand. You normally never need it.)
5. Open the email in Gmail, choose "Show original", and confirm `SPF: PASS` and `DKIM: PASS`.
6. Only then clear `SEND_ALLOWLIST` to go live. The rules (business hours in the lead's country, 5 emails a day at first, spacing, never twice) apply automatically.
Telegram controls: `/pause`, `/resume`, `/status`, `/queue`, `/stats`, `/suppress`. Type `/` in the chat to see the menu.

## The card limit and `/queue`
Telegram never shows more than 10 cards at once (`MAX_OPEN_CARDS` in `.env`). If more are ready, they wait quietly, and the next card appears as soon as you approve, reject or change one. Follow-ups come before new first emails. Type `/queue` in the chat to see how many are waiting for you and how many are queued behind them. A follow-up card is short: which email it is, what the previous email was, the new text, and the evidence it is based on. The signature is the same as email 1 and is not repeated.

## Follow-ups
You do not schedule anything. When a lead has not replied, the system writes the next email once the wait is over (3, then 5, then 9 working days in the lead's country), and a card appears in Telegram. You approve it like the first one. `.\zypher.ps1 draft` checks for due follow-ups, and the worker checks about every 30 minutes. They are sent as replies in the same thread. The sequence stops by itself when someone replies, bounces or opts out, and after the fourth email it never contacts them again.

## Is it working? `stats`
`.\zypher.ps1 stats` (or `/stats` in Telegram) shows, for the last 7 days and all time: how many companies you emailed, how many replied (and the percentage), how many replies were positive, bounces, opt-outs and complaints, with a plain sentence on what it means. Typical cold email gets about 3 to 5% replies. Under 100 emails the numbers are too small to judge. If fewer than 1% replied after 300 companies, you get a Telegram alert: change the offer or the kind of companies, do not send more of the same. Meetings booked are not counted: keep that number yourself.

## Where to see what was sent
Every email the system sends is also saved in your mailbox's **Sent** folder (Titan or webmail), so you can see exactly what went out. Telegram also posts "✅ Sent to <address>" for each one, and `.\zypher.ps1 stats` counts them. Only emails sent after this feature was added appear in Sent; earlier test emails do not. If the Sent copy ever fails, the email was still sent, and the problem is logged.

## What happens after an email is sent
Every minute the worker reads your mailbox (read-only, and only mail about the emails we sent):
- **A bounce:** the address is added to the do-not-contact list and you get an alert. Two bounces in a short run pause all sending automatically.
- **"No thanks" or "unsubscribe":** added to the do-not-contact list at once, and you are told.
- **"This is spam":** added to the list and sending is paused.
- **Any other reply:** the follow-ups stop and the full text is sent to your Telegram with a label (interested, question, not now...). You answer it yourself from your own mailbox.
- **Out-of-office:** noted, nothing else happens.
Mail that has nothing to do with our emails is never stored, logged or shown. `.\zypher.ps1 inbox` reads the mailbox once by hand.

## Other commands
- `.\zypher.ps1 stats` shows replies, bounces and opt-outs. `.\zypher.ps1 preflight` checks your mail setup before the first send. `.\zypher.ps1 testmail` is a Python command (`python -m zypher_agent testmail --to you@gmail.com`) that sends one test email to an address you own.
- `.\zypher.ps1 refresh` rewrites every draft still waiting with the latest rules (use after changing wording rules).
- `.\zypher.ps1 demo` runs everything on fake data, free, with no keys. `.\zypher.ps1 test` runs the self-checks.

## Running on the server
Once the server is set up (docs/runbooks/server-setup.md), the agent runs there all the time, in Docker, and you do not open a terminal for it. Telegram works exactly as before.

**What it does by itself**
- Everything `start` does: Telegram buttons, sending in each lead's business hours, inbox reading, follow-ups.
- **Drafts new leads every weekday at 08:00 IST** (`DRAFT_DAILY_LIMIT`, `DRAFT_TIME`, `DRAFT_DAYS` in the server's `.env`). The cards arrive in Telegram as usual. If the server was down at 08:00, it drafts when it comes back that same day, never a missed day later.
- **Does NOT find new leads.** That costs Apify credit, so it only runs when you ask: `.\zypher.ps1 find ...` on the laptop still works while the server runs (it only adds leads to the database).
- Restarts itself after a crash, a hang or a server reboot. If it keeps crashing you get one Telegram message an hour about it.
- Backs up the database every night at 02:30 IST, keeping 14 days.
- Tells Healthchecks.io it is alive every 5 minutes. If it goes quiet for 20 minutes (server down, anything), Healthchecks emails you.

**Only one copy at a time.** The laptop and the server must never both run `start`: Telegram taps would go to either one at random. The agent enforces this. A second copy prints "Another copy of the agent is already running… waiting" (and tells you once in Telegram) and does nothing until the first one stops. So on the laptop, use `status`, `stats`, `find` and `draft` freely; do not use `start` while the server runs.

**Going back to the laptop** (if the server has a problem): on the server run `docker compose stop sales-agent`, then on the laptop `.\zypher.ps1 start`. Everything is in the database, so nothing is lost. If the server is simply gone, the laptop copy takes over by itself within a couple of minutes.

**Useful server commands** (in the project folder on the server, `/opt/zypher/zypher-sales-agent`)

| Command | What it does |
|---|---|
| `docker compose ps` | Is it running, and `healthy`? |
| `docker compose logs -f sales-agent` | Watch it live (Ctrl+C stops watching, not the agent) |
| `docker compose logs --tail 100 sales-agent` | The last 100 lines |
| `git pull && docker compose up -d --build` | Install a code update |
| `docker compose stop sales-agent` / `start sales-agent` | Pause / resume the whole agent |
| `docker compose exec sales-agent python -m zypher_agent source --query "trading company" --location "Dubai, United Arab Emirates" --max 20` | Find leads from the server (costs Apify credit) |
| `ls -lh backups/` | The nightly backups |

## If something looks wrong
- Telegram says "Another copy of the agent is already running": the laptop and the server are both trying to run. Stop the one you do not want (see "Running on the server").
- "No usable email" leads are parked as `nurture`: the site shows no public address we trust. Call or use LinkedIn by hand.
- A card says FAILED checks: the evidence on their site was too thin for a specific email. Skip it, or use Suggest changes with a detail you know.
- Telegram buttons do nothing: nothing is running. On the server: `docker compose ps`; on the laptop: `.\zypher.ps1 start`.
- Apify "not enough credit" (HTTP 402): add credit or wait for the monthly reset.
- Model "rate limit": the free model allows about one lead a minute. The agent waits or switches to a backup model by itself.

## Rules the system enforces for you (you cannot turn these off by accident)
Every email carries your identity, opt-out line and postal address. Anyone on the do-not-contact list is skipped at every stage. No email goes to a guessed address. No client names, results or prices appear in a first touch.
