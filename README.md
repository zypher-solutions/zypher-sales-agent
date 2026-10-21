# Zypher Sales Agent

An AI-assisted outbound sales system for **Zypher Software Solutions LLP**. It finds SME leads, researches each business, drafts personalized outreach around **business-process automation**, sends it through compliant channels, handles replies, and hands warm leads to a human.

Owner: Muhammed Hasheem (CEO / technical lead)
Status: Phases 1 and 2 built (research, drafting, guardrails, Telegram approval, sending, inbox, follow-ups, stats); live sending has started (2 leads emailed by 2026-10-21, staged with an allowlist). Now moving to 24/7 hosting in Docker on the Oracle free server (docs/plans/step1-server.md). Replaces the n8n workflow "Cold Outreach - Phase 1".

**Product direction:** this is built as a standalone, sellable sales-automation product for SMEs (hotels, restaurants, agencies, clinics, etc.). Zypher is "client zero". See section 10.

---

## 1. Why we are rebuilding Phase 1

Phase 1 proved the loop (sheet -> website -> LLM draft -> Telegram approval -> SMTP send). It does not scale or sell our new positioning.

| Problem in Phase 1 | Consequence |
|---|---|
| Pitch is "improve your website" | Zypher now leads with **business automation**; web/app/CRM is secondary |
| Research reads only `<title>` and meta description | Emails are shallow; no real insight into workflows or pain |
| `llama3:8b` locally, with regex cleanup of its output | Inconsistent quality; brittle post-processing of "Here is the email..." |
| Google Sheet is the database | No history, no dedupe, race conditions, no analytics |
| No follow-ups, no reply handling | Most replies come from follow-ups 2 to 4 |
| No suppression/unsubscribe list, no opt-out footer | Legal exposure and spam-complaint risk |
| Sending from the main company domain, no rate limits | One bad batch can damage `zypher-solutions.com` deliverability |
| Single channel, single human approval path | Cannot add LinkedIn/WhatsApp or move to auto-send safely |
| Hardcoded chat IDs, no tests, no logs | Hard to debug or hand to a partner |

## 2. Goals and non-goals

**Goals (v1)**
- Source and enrich SME leads in UAE, India, Qatar, and US.
- Research each lead (site, services, tech signals, likely manual workflows) and produce a grounded "automation opportunity" hypothesis.
- Draft outreach for **email, LinkedIn (human-assist), WhatsApp**, with multi-step follow-up sequences.
- Classify replies (interested / not now / unsubscribe / out-of-office / wrong person) and route them.
- Staged autonomy: human approval first, auto-send only after quality gates are met.
- Push qualified leads into the **Notion CRM** and notify via **Slack** (the partners' existing workflow).
- Full audit trail and metrics in Postgres.

**Non-goals (v1)**
- Voice / calling agent (Phase 4).
- Fully automated LinkedIn actions (connection requests/DMs through bots risks account bans; we draft, a human sends).
- Large enterprises.
- Building a general-purpose CRM.

## 3. Architecture

```
          +-----------------+
 Sources  | Lead import     |  CSV / Apollo / Maps / manual
          +--------+--------+
                   v
          +-----------------+      +--------------------+
          | Enrichment &    |----->| Supabase (Postgres)|  source of truth
          | validation      |      | leads, messages,   |
          +--------+--------+      | events, suppression|
                   v               +---------+----------+
          +-----------------+                ^
          | Research agent  |  scrape site, extract services,
          | (Claude + tools)|  tech stack, workflow pain hypotheses
          +--------+--------+
                   v
          +-----------------+
          | Drafting agent  |  per channel, per sequence step
          +--------+--------+
                   v
          +-----------------+      Telegram / Slack approval (Level 0-1)
          | QA + guardrails |----> or auto-send (Level 2+)
          +--------+--------+
                   v
          +-----------------+
          | Sender / queue  |  rate limits, send windows, warmup
          +--------+--------+
                   v
          +-----------------+
          | Reply handler   |  classify -> Notion CRM + Slack
          +-----------------+
```

**Stack**
- Python 3.11+, FastAPI, Pydantic
- Claude API with tool use for research/drafting/classification (model configurable via env)
- Supabase Postgres as the single source of truth
- Background jobs: start with a simple worker plus Postgres job table; move to Celery/RQ only if needed
- n8n kept only for glue (Notion/Slack triggers), not for core logic
- Delivery: Google Workspace/SMTP or a cold-email platform (decision pending, see section 8)

## 4. Autonomy levels (staged)

| Level | Behavior | Promotion criteria |
|---|---|---|
| 0 | Agent drafts, human approves/edits every message | Starting point |
| 1 | Human approves only first touch; follow-ups auto-send | 100+ approved sends, under 10% edited heavily |
| 2 | Auto-send to leads scoring above a threshold; human reviews the rest | Bounce under 2%, spam complaints under 0.1%, no policy violations |
| 3 | Fully autonomous within guardrails; human reviews warm replies only | Sustained 4+ weeks at Level 2 with clean metrics |

Replies that are anything other than clear negatives always go to a human.

## 5. Compliance and deliverability (non-negotiable)

- **Separate sending domain(s)** for cold email (not the main company domain), with SPF, DKIM, DMARC, and warmup.
- Daily caps per mailbox, ramped gradually; send only in the recipient's business hours.
- Every email carries a clear opt-out and the sender's identity and postal address. Unsubscribes are honored immediately via the suppression list.
- Regional rules: CAN-SPAM (US), GDPR/ePrivacy where relevant, UAE and Qatar data and anti-spam rules, India's DPDP Act and TRAI rules for messaging. **Verify current rules per country before launch.**
- **WhatsApp:** use the official WhatsApp Business API with approved templates; no cold-blasting from personal numbers. Prefer WhatsApp for leads that have engaged or provided a number publicly for business.
- **LinkedIn:** the agent prepares research and drafts; a human sends.
- The agent never invents facts about a lead. Claims must trace to scraped evidence stored with the draft.

## 6. Repository layout (target)

```
zypher-sales-agent/
  README.md
  CLAUDE.md            # instructions for AI coding sessions on this repo
  pyproject.toml
  .env.example
  schema.sql           # Supabase schema
  icp.yaml             # ideal customer profile + offer + messaging rules
  scripts/import_leads.py
  src/zypher_agent/
    config.py
    db.py
    research/          # scraping + analysis agent
    drafting/          # per-channel drafting
    guardrails/        # QA checks, banned claims, compliance
    channels/          # email.py, whatsapp.py, linkedin.py
    replies/           # classification + routing
    integrations/      # notion.py, slack.py, telegram.py
    worker.py
  tests/
  docs/legacy/Cold Outreach - Phase 1.json
```

## 7. Roadmap

| Phase | Scope |
|---|---|
| 0 | Plan, schema, ICP, import existing sheet into Postgres (this repo's current state) |
| 1 | Research agent + email drafting + Telegram approval (parity with n8n, better quality) |
| 2 | Sending infrastructure: sending domain, warmup, queue, rate limits, suppression, follow-up sequences |
| 2.5 | Runs unattended: Docker on the Oracle free ARM server, daily drafting schedule, one-copy lock, health checks, nightly backups (Step 1 of ../START_HERE.md) |
| 3 | Reply classification, Notion CRM + Slack handoff, metrics dashboard |
| 4 | WhatsApp Business API + LinkedIn assist; staged autonomy promotion |
| 5 | Voice agent, A/B testing, lead scoring from outcomes |

## 8. Open decisions

1. Lead sources and budget (Apollo, Apify/Google Maps scraping, manual lists).
2. Email delivery: own mailboxes on a separate domain vs Instantly/Smartlead-style platform.
3. Sending domain name and number of mailboxes.
4. Notion CRM database structure (needs a shared example).
5. Case studies / proof points we may legally cite in emails.
6. Pricing and offer: free workflow audit? fixed-price pilot?
7. ~~Hosting for the agent~~ Decided 2026-10-21: Docker on the Oracle Cloud free ARM server now, DigitalOcean when a paying client depends on it (../platform/hosting.md).

## 9. Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
pip install -e ".[dev]"  # includes pytest
cp .env.example .env     # fill in values
# Run schema.sql, then migrations/001_phase1.sql, in the Supabase SQL editor

pytest                                        # all tests, no keys needed
python -m zypher_agent run --dry-run          # whole pipeline with fake sites + mocked LLM
python scripts/import_leads.py leads.csv --dry-run
python scripts/import_leads.py leads.csv      # real import (needs DATABASE_URL)
python -m zypher_agent run --limit 5          # research + draft new leads, cards to Telegram
python -m zypher_agent bot                    # listen for Approve / Suggest changes / Reject
```

To run the Postgres contract tests, point `TEST_DATABASE_URL` at a throwaway database (they delete rows where `source = 'test'`).

Docker (same image on the laptop and the arm64 server; server guide: docs/runbooks/server-setup.md):

```bash
docker compose up -d --build                  # agent + nightly backup, restarts by itself
docker compose ps                             # running? healthy?
docker compose logs -f sales-agent
docker compose -f docker-compose.yml -f docker-compose.local.yml up --build sales-agent   # laptop test: never sends
```

Only one copy of `start` runs at a time (a database lock): never expect the laptop and the server to share the work.

## 10. Security notes

- Never commit `.env`. Rotate any credentials that were exposed in shared workflow exports.
- Telegram/Slack approvals must verify the sender's user ID against an allowlist.
- Scraped content is untrusted data and must never be treated as instructions by the agents (prompt-injection risk from lead websites).

## 10. Product vision: from internal tool to SaaS

Zypher is tenant #1. Other SMEs will later get the same agent configured for their own business. The goal is a **complete commercial product that a non-technical owner can run without help**.

### Design principles
1. **Multi-tenant from the data model up.** Every table carries `tenant_id`; every query is scoped by it (Postgres row-level security as the backstop). Retrofitting this later is far costlier than doing it now.
2. **Everything Zypher-specific becomes tenant config**, not code: ICP, offer, tone, banned claims, sender identity, send windows, channels, autonomy level (today `icp.yaml` and `.env`).
3. **Guardrails are platform-level and cannot be disabled by a tenant**: suppression list, opt-out footer, rate limits, evidence-backed claims. These protect every tenant's domain and our liability.
4. **Non-technical UX**: no YAML, no `.env`, no terminal. Setup is a wizard; daily use is a review inbox.
5. **Per-tenant isolation of sending**: each tenant uses its own domain and mailboxes. Never share sending infrastructure across tenants (one tenant's spam would burn everyone).
6. **Cost metering per tenant** (LLM calls are already logged with cost), the basis for pricing.

### Feature set for the commercial product

| Area | Features |
|---|---|
| Onboarding | Guided setup wizard: business profile, "what do you sell", ideal customer, tone, proof points; paste website URL and the agent pre-fills the profile for review; sending-domain checklist with live SPF/DKIM/DMARC checks |
| Leads | CSV upload, manual add, optional Apollo/Google Maps import; dedupe, validation, enrichment; per-lead research summary showing the evidence behind each claim |
| Campaigns | Pick an audience + offer + channels; multi-step sequences with plain-language editing; preview before launch |
| Review inbox | Mobile-friendly approve / edit / reject queue for drafts and replies; one-tap actions; Telegram/WhatsApp/email notifications |
| Sending | Domain warmup schedule, daily caps, business-hours windows per lead timezone, automatic pause on bounce/complaint spikes |
| Replies | Auto-classification, suggested responses, hand-off to human for anything not a clear negative, meeting-link insertion |
| Compliance | Automatic opt-out footer, global + per-tenant suppression, consent/source tracking per lead, audit log, data export and deletion (DPDP/GDPR) |
| Autonomy | Staged levels 0-3 with in-app promotion criteria and metrics (see section 4) |
| Analytics | Plain-language dashboard: sent, replied, meetings booked, cost per meeting; no jargon |
| Integrations | CRM sync (Notion first, then HubSpot/Zoho), Slack/Telegram alerts, calendar booking (Calendly/Google) |
| Admin / billing | Team members and roles, usage metering, subscription billing (Razorpay for India, Stripe elsewhere), support tooling |
| Channels | Email first; WhatsApp via official Business API templates; LinkedIn as human-assist drafts only |

### Staging (do not build it all at once)
- **Now (Phases 2-3):** make the engine's features the best they can be, running for Zypher. Architecture stays business-agnostic: config-driven prompts and policy, `tenant_id` on every table, swappable integrations. **No payments, billing, domain management, or UI yet.**
- **Later:** admin web app (onboarding wizard, review inbox, dashboard), then self-serve signup, billing (Razorpay/Stripe), custom sending-domain management, and industry template packs.
- Rule: the later items are deliberately deferred, not forgotten. The config/`tenant_id`/interface rules in `CLAUDE.md` exist so adding them needs no rewrite.

### Risks to decide on early
- **Liability:** we would send outreach on behalf of customers. Terms of service must make the tenant the sender of record and responsible for lawful lead sources; our guardrails must enforce opt-out regardless.
- **Industry fit:** hotels and restaurants usually rely on inbound/B2C, so cold email may be the wrong core feature for them. Validate which SME segments actually do outbound B2B before building vertical templates.
- **Data protection:** we will hold our customers' lead data; DPDP/GDPR processor obligations apply.

## 11. What we need from the owner (credentials and decisions)

Secrets go only in `.env` (local) or the host's secret store. Never paste them into chat, commits, or issues.

### Required now (to run Phase 1 for real)
| Item | Where to get it | Env var |
|---|---|---|
| Anthropic API key | console.anthropic.com -> API keys | `ANTHROPIC_API_KEY` |
| Supabase Postgres URI | Supabase project -> Settings -> Database -> Connection string (URI). Then run `schema.sql` and `migrations/001_phase1.sql` in the SQL editor | `DATABASE_URL` |
| Telegram bot token | Message @BotFather -> `/newbot` | `TELEGRAM_BOT_TOKEN` |
| Telegram user ID(s) allowed to approve | Message @userinfobot; one ID per approver, comma-separated | `TELEGRAM_ALLOWED_USER_IDS` |
| Initial lead list | CSV with company, website, email, country, name (see `scripts/import_leads.py`) | n/a |

### Required for Phase 2 (sending)
| Item | Notes | Env var |
|---|---|---|
| Separate sending domain | Register one (not the main company domain); set up SPF, DKIM, DMARC | n/a |
| Mailbox(es) on that domain | Google Workspace or similar; app password or SMTP credentials | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SENDER_EMAIL` |
| Sender identity | Name, registered postal address of the LLP, opt-out wording | `SENDER_NAME`, `SENDER_POSTAL_ADDRESS`, `OPT_OUT_LINE` |
| Public unsubscribe URL | A small hosted endpoint we build; needs a host (see below) | `UNSUBSCRIBE_BASE_URL` |
| Hosting | Oracle Cloud free ARM server, Docker Compose (decided 2026-10-21) | n/a |

### Required for Phase 3+ (optional until then)
| Item | Env var |
|---|---|
| Slack bot token + channel ID | `SLACK_BOT_TOKEN`, `SLACK_CHANNEL_ID` |
| Notion integration token + CRM database ID (share the database with the integration; send an example of its structure) | `NOTION_TOKEN`, `NOTION_CRM_DATABASE_ID` |
| WhatsApp Business API token + phone number ID (Meta Business verification needed) | `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID` |
| Apollo API key (if used for lead sourcing) | `APOLLO_API_KEY` |

### Added for the SaaS direction (later)
Product name and domain; payment provider accounts (Razorpay, Stripe); an auth provider (Supabase Auth is enough); a transactional email provider for system mail (invites, receipts) kept separate from cold-email mailboxes; legal review of ToS, privacy policy, and data-processing terms.

### Decisions still open
Items 1-7 in section 8, plus: SaaS product name, first target segment beyond Zypher, and pricing model (per seat, per lead, or per meeting booked).
